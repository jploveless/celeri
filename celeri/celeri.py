import addict
import copy
import datetime
import h5py
import json
import meshio
import scipy
import pyproj
import pytest
import os
import matplotlib.pyplot as plt
import warnings
import numpy as np
import pandas as pd
import okada_wrapper
import cutde.halfspace as cutde_halfspace
from ismember import ismember
from loguru import logger
from tqdm.notebook import tqdm
from typing import List, Dict, Tuple

from . import celeri_closure
from .celeri_util import sph2cart, cart2sph

# Global constants
GEOID = pyproj.Geod(ellps="WGS84")
KM2M = 1.0e3
M2MM = 1.0e3
RADIUS_EARTH = np.float64((GEOID.a + GEOID.b) / 2)
DEG_PER_MYR_TO_RAD_PER_YR = 1 / 1e6  # TODO: What should this conversion be?
N_MESH_DIM = 3

# Set up logging to file:
# TODO: Revisit logging
# logger.remove()  # Remove any existing loggers includeing default stderr
# logger.add(RUN_NAME + ".log")
# logger.info("RUN_NAME: " + RUN_NAME)


@pytest.mark.skip(reason="Writing output to disk")
def create_output_folder(command: Dict):
    # Check to see if "runs" folder exists and if not create it
    if not os.path.exists(command.base_runs_folder):
        os.mkdir(command.base_runs_folder)

    # Make output folder for current run
    os.mkdir(command.output_path)


def get_mesh_perimeter(meshes):
    for i in range(len(meshes)):
        x_coords = meshes[i].meshio_object.points[:, 0]
        y_coords = meshes[i].meshio_object.points[:, 1]
        meshes[i].x_perimeter = x_coords[meshes[i].ordered_edge_nodes[:, 0]]
        meshes[i].y_perimeter = y_coords[meshes[i].ordered_edge_nodes[:, 0]]
        meshes[i].x_perimeter = np.append(
            meshes[i].x_perimeter, x_coords[meshes[i].ordered_edge_nodes[0, 0]]
        )
        meshes[i].y_perimeter = np.append(
            meshes[i].y_perimeter, y_coords[meshes[i].ordered_edge_nodes[0, 0]]
        )


def read_data(command_file_name: str):
    # Read command data
    with open(command_file_name, "r") as f:
        command = json.load(f)
    command = addict.Dict(command)  # Convert to dot notation dictionary
    command.file_name = command_file_name

    # Add run_name and output_path
    command.run_name = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    command.output_path = os.path.join(command.base_runs_folder, command.run_name)

    # Read segment data
    segment = pd.read_csv(command.segment_file_name)
    segment = segment.loc[:, ~segment.columns.str.match("Unnamed")]

    # Read block data
    block = pd.read_csv(command.block_file_name)
    block = block.loc[:, ~block.columns.str.match("Unnamed")]

    # Read mesh data - List of dictionary version
    with open(command.mesh_parameters_file_name) as f:
        mesh_param = json.load(f)

    meshes = []
    if len(mesh_param) > 0:
        for i in range(len(mesh_param)):
            meshes.append(addict.Dict())
            meshes[i].meshio_object = meshio.read(mesh_param[i]["mesh_filename"])
            meshes[i].verts = meshes[i].meshio_object.get_cells_type("triangle")

            # Expand mesh coordinates
            meshes[i].lon1 = meshes[i].meshio_object.points[meshes[i].verts[:, 0], 0]
            meshes[i].lon2 = meshes[i].meshio_object.points[meshes[i].verts[:, 1], 0]
            meshes[i].lon3 = meshes[i].meshio_object.points[meshes[i].verts[:, 2], 0]
            meshes[i].lat1 = meshes[i].meshio_object.points[meshes[i].verts[:, 0], 1]
            meshes[i].lat2 = meshes[i].meshio_object.points[meshes[i].verts[:, 1], 1]
            meshes[i].lat3 = meshes[i].meshio_object.points[meshes[i].verts[:, 2], 1]
            meshes[i].dep1 = meshes[i].meshio_object.points[meshes[i].verts[:, 0], 2]
            meshes[i].dep2 = meshes[i].meshio_object.points[meshes[i].verts[:, 1], 2]
            meshes[i].dep3 = meshes[i].meshio_object.points[meshes[i].verts[:, 2], 2]
            meshes[i].centroids = np.mean(
                meshes[i].meshio_object.points[meshes[i].verts, :], axis=1
            )
            # Cartesian coordinates in meters
            meshes[i].x1, meshes[i].y1, meshes[i].z1 = sph2cart(
                meshes[i].lon1,
                meshes[i].lat1,
                RADIUS_EARTH + KM2M * meshes[i].dep1,
            )
            meshes[i].x2, meshes[i].y2, meshes[i].z2 = sph2cart(
                meshes[i].lon2,
                meshes[i].lat2,
                RADIUS_EARTH + KM2M * meshes[i].dep2,
            )
            meshes[i].x3, meshes[i].y3, meshes[i].z3 = sph2cart(
                meshes[i].lon3,
                meshes[i].lat3,
                RADIUS_EARTH + KM2M * meshes[i].dep3,
            )

            # Cartesian triangle centroids
            meshes[i].x_centroid = (meshes[i].x1 + meshes[i].x2 + meshes[i].x3) / 3.0
            meshes[i].y_centroid = (meshes[i].y1 + meshes[i].y2 + meshes[i].y3) / 3.0
            meshes[i].z_centroid = (meshes[i].z1 + meshes[i].z2 + meshes[i].z3) / 3.0

            # Cross products for orientations
            tri_leg1 = np.transpose(
                [
                    np.deg2rad(meshes[i].lon2 - meshes[i].lon1),
                    np.deg2rad(meshes[i].lat2 - meshes[i].lat1),
                    (1 + KM2M * meshes[i].dep2 / RADIUS_EARTH)
                    - (1 + KM2M * meshes[i].dep1 / RADIUS_EARTH),
                ]
            )
            tri_leg2 = np.transpose(
                [
                    np.deg2rad(meshes[i].lon3 - meshes[i].lon1),
                    np.deg2rad(meshes[i].lat3 - meshes[i].lat1),
                    (1 + KM2M * meshes[i].dep3 / RADIUS_EARTH)
                    - (1 + KM2M * meshes[i].dep1 / RADIUS_EARTH),
                ]
            )
            meshes[i].nv = np.cross(tri_leg1, tri_leg2)
            azimuth, elevation, r = cart2sph(
                meshes[i].nv[:, 0],
                meshes[i].nv[:, 1],
                meshes[i].nv[:, 2],
            )
            meshes[i].strike = wrap2360(-np.rad2deg(azimuth))
            meshes[i].dip = 90 - np.rad2deg(elevation)
            meshes[i].dip_flag = meshes[i].dip != 90
            meshes[i].smoothing_weight = mesh_param[i]["smoothing_weight"]
            meshes[i].n_eigenvalues = mesh_param[i]["n_eigenvalues"]
            meshes[i].top_slip_rate_constraint = mesh_param[i][
                "top_slip_rate_constraint"
            ]
            meshes[i].bot_slip_rate_constraint = mesh_param[i][
                "bot_slip_rate_constraint"
            ]
            meshes[i].side_slip_rate_constraint = mesh_param[i][
                "side_slip_rate_constraint"
            ]
            meshes[i].n_tde = meshes[i].lon1.size
            get_mesh_edge_elements(meshes)
        get_mesh_perimeter(meshes)

    # Read station data
    if (
        not command.__contains__("station_file_name")
        or len(command.station_file_name) == 0
    ):
        station = pd.DataFrame(
            columns=[
                "lon",
                "lat",
                "corr",
                "other1",
                "name",
                "east_vel",
                "north_vel",
                "east_sig",
                "north_sig",
                "flag",
                "up_vel",
                "up_sig",
                "east_adjust",
                "north_adjust",
                "up_adjust",
                "depth",
                "x",
                "y",
                "z",
                "block_label",
            ]
        )
    else:
        station = pd.read_csv(command.station_file_name)
        station = station.loc[:, ~station.columns.str.match("Unnamed")]

    # Read Mogi source data
    if not command.__contains__("mogi_file_name") or len(command.mogi_file_name) == 0:
        mogi = pd.DataFrame(
            columns=[
                "name",
                "lon",
                "lat",
                "depth",
                "volume_change_flag",
                "volume_change",
                "volume_change_sig",
            ]
        )
    else:
        mogi = pd.read_csv(command.mogi_file_name)
        mogi = mogi.loc[:, ~mogi.columns.str.match("Unnamed")]

    # Read SAR data
    if not command.__contains__("sar_file_name") or len(command.sar_file_name) == 0:
        sar = pd.DataFrame(
            columns=[
                "lon",
                "lat",
                "depth",
                "line_of_sight_change_val",
                "line_of_sight_change_sig",
                "look_vector_x",
                "look_vector_y",
                "look_vector_z",
                "reference_point_x",
                "reference_point_y",
            ]
        )

    else:
        sar = pd.read_csv(command.sar_file_name)
        sar = sar.loc[:, ~sar.columns.str.match("Unnamed")]
    return command, segment, block, meshes, station, mogi, sar


def wrap2360(lon):
    lon[np.where(lon < 0.0)] += 360.0
    return lon


def process_station(station, command):
    if command.unit_sigmas == "yes":  # Assign unit uncertainties, if requested
        station.east_sig = np.ones_like(station.east_sig)
        station.north_sig = np.ones_like(station.north_sig)
        station.up_sig = np.ones_like(station.up_sig)

    station["depth"] = np.zeros_like(station.lon)
    station["x"], station["y"], station["z"] = sph2cart(
        station.lon, station.lat, RADIUS_EARTH
    )
    station = station.drop(np.where(station.flag == 0)[0])
    station = station.reset_index(drop=True)
    return station


def locking_depth_manager(segment, command):
    """
    This function assigns the locking depths given in the command file to any
    segment that has the same locking depth flag.  Segments with flag =
    0, 1 are untouched.
    """
    segment = segment.copy(deep=True)
    segment.locking_depth.values[
        segment.locking_depth_flag == 2
    ] = command.locking_depth_flag2
    segment.locking_depth.values[
        segment.locking_depth_flag == 3
    ] = command.locking_depth_flag3
    segment.locking_depth.values[
        segment.locking_depth_flag == 4
    ] = command.locking_depth_flag4
    segment.locking_depth.values[
        segment.locking_depth_flag == 5
    ] = command.locking_depth_flag5

    if command.locking_depth_override_flag == "yes":
        segment.locking_depth.values = command.locking_depth_override_value
    return segment


def zero_mesh_segment_locking_depth(segment, meshes):
    """
    This function sets the locking depths of any segments that trace
    a mesh to zero, so that they have no rectangular elastic strain
    contribution, as the elastic strain is accounted for by the mesh.

    To have its locking depth set to zero, the segment's patch_flag
    and patch_file_name fields must not be equal to zero but also
    less than the number of available mesh files.
    """
    toggle_off = np.where(
        (segment.patch_flag != 0)
        & (segment.patch_file_name != 0)
        & (segment.patch_file_name <= len(meshes))
    )[0]
    segment.locking_depth.values[toggle_off] = 0
    return segment


def order_endpoints_sphere(segment):
    """
    Endpoint ordering function, placing west point first.
    This converts the endpoint coordinates from spherical to Cartesian,
    then takes the cross product to test for ordering (i.e., a positive z
    component of cross(point1, point2) means that point1 is the western
    point). This method works for both (-180, 180) and (0, 360) longitude
    conventions.
    BJM: Not sure why cross product approach was definitely not working in
    python so I revereted to relative longitude check which sould be fine because
    we're always in 0-360 space.
    """
    segment_copy = copy.deepcopy(segment)
    endpoints1 = np.transpose(np.array([segment.x1, segment.y1, segment.z1]))
    endpoints2 = np.transpose(np.array([segment.x2, segment.y2, segment.z2]))
    cross_product = np.cross(endpoints1, endpoints2)
    swap_endpoint_idx = np.where(cross_product[:, 2] < 0)
    segment_copy.lon1.values[swap_endpoint_idx] = segment.lon2.values[swap_endpoint_idx]
    segment_copy.lat1.values[swap_endpoint_idx] = segment.lat2.values[swap_endpoint_idx]
    segment_copy.lon2.values[swap_endpoint_idx] = segment.lon1.values[swap_endpoint_idx]
    segment_copy.lat2.values[swap_endpoint_idx] = segment.lat1.values[swap_endpoint_idx]
    return segment_copy


def segment_centroids(segment):
    """Calculate segment centroids."""
    segment["centroid_x"] = np.zeros_like(segment.lon1)
    segment["centroid_y"] = np.zeros_like(segment.lon1)
    segment["centroid_z"] = np.zeros_like(segment.lon1)
    segment["centroid_lon"] = np.zeros_like(segment.lon1)
    segment["centroid_lat"] = np.zeros_like(segment.lon1)

    for i in range(len(segment)):
        segment_forward_azimuth, _, _ = GEOID.inv(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i]
        )
        segment_down_dip_azimuth = segment_forward_azimuth + 90.0 * np.sign(
            np.cos(np.deg2rad(segment.dip[i]))
        )
        azimuth_xy_cartesian = (segment.y2[i] - segment.y1[i]) / (
            segment.x2[i] - segment.x1[i]
        )
        azimuth_xy_cartesian = np.arctan(-1.0 / azimuth_xy_cartesian)
        segment.centroid_z.values[i] = (
            segment.locking_depth[i] - segment.burial_depth[i]
        ) / 2.0
        segment_down_dip_distance = segment.centroid_z[i] / np.abs(
            np.tan(np.deg2rad(segment.dip[i]))
        )
        (
            segment.centroid_lon.values[i],
            segment.centroid_lat.values[i],
            _,
        ) = GEOID.fwd(
            segment.mid_lon[i],
            segment.mid_lat[i],
            segment_down_dip_azimuth,
            segment_down_dip_distance,
        )
        segment.centroid_x.values[i] = segment.mid_x[i] + np.sign(
            np.cos(np.deg2rad(segment.dip[i]))
        ) * segment_down_dip_distance * np.cos(azimuth_xy_cartesian)
        segment.centroid_y.values[i] = segment.mid_y[i] + np.sign(
            np.cos(np.deg2rad(segment.dip[i]))
        ) * segment_down_dip_distance * np.sin(azimuth_xy_cartesian)
    segment.centroid_lon.values[segment.centroid_lon < 0.0] += 360.0
    return segment


def process_segment(segment, command, meshes):
    """
    Add derived fields to segment dataframe
    """

    segment["length"] = np.zeros(len(segment))
    for i in range(len(segment)):
        _, _, segment.length.values[i] = GEOID.inv(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i]
        )  # Segment length in meters

    segment["x1"], segment["y1"], segment["z1"] = sph2cart(
        segment.lon1, segment.lat1, RADIUS_EARTH
    )
    segment["x2"], segment["y2"], segment["z2"] = sph2cart(
        segment.lon2, segment.lat2, RADIUS_EARTH
    )

    segment = order_endpoints_sphere(segment)

    # This calculation needs to account for the periodic nature of longitude.
    # Calculate the periodic longitudinal separation.
    # @BJM: Is this better done with GEIOD?
    sep = segment.lon2 - segment.lon1
    periodic_lon_separation = np.where(
        sep > 180, sep - 360, np.where(sep < -180, sep + 360, sep)
    )
    segment["mid_lon_plate_carree"] = (
        segment.lon1.values + periodic_lon_separation / 2.0
    )

    # No worries for latitude because there's no periodicity.
    segment["mid_lat_plate_carree"] = (segment.lat1.values + segment.lat2.values) / 2.0
    segment["mid_lon"] = np.zeros_like(segment.lon1)
    segment["mid_lat"] = np.zeros_like(segment.lon1)

    for i in range(len(segment)):
        segment.mid_lon.values[i], segment.mid_lat.values[i] = GEOID.npts(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i], 1
        )[0]
    segment.mid_lon.values[segment.mid_lon < 0.0] += 360.0

    segment["mid_x"], segment["mid_y"], segment["mid_z"] = sph2cart(
        segment.mid_lon, segment.mid_lat, RADIUS_EARTH
    )
    segment = locking_depth_manager(segment, command)
    segment = zero_mesh_segment_locking_depth(segment, meshes)
    segment = segment_centroids(segment)
    return segment


def polygon_area(x, y):
    """
    From: https://newbedev.com/calculate-area-of-polygon-given-x-y-coordinates
    """
    return 0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


def assign_block_labels(segment, station, block, mogi, sar):
    """
    Ben Thompson's implementation of the half edge approach to the
    block labeling problem and east/west assignment.
    """
    # segment = split_segments_crossing_meridian(segment)

    np_segments = np.zeros((len(segment), 2, 2))
    np_segments[:, 0, 0] = segment.lon1.to_numpy()
    np_segments[:, 1, 0] = segment.lon2.to_numpy()
    np_segments[:, 0, 1] = segment.lat1.to_numpy()
    np_segments[:, 1, 1] = segment.lat2.to_numpy()

    closure = celeri_closure.run_block_closure(np_segments)
    labels = celeri_closure.get_segment_labels(closure)

    segment["west_labels"] = labels[:, 0]
    segment["east_labels"] = labels[:, 1]

    # Check for unprocessed indices
    unprocessed_indices = np.union1d(
        np.where(segment["east_labels"] < 0),
        np.where(segment["west_labels"] < 0),
    )
    if len(unprocessed_indices) > 0:
        print("Found unproccessed indices")

    # Find relative areas of each block to identify an external block
    block["area_steradians"] = -1 * np.ones(len(block))
    block["area_plate_carree"] = -1 * np.ones(len(block))
    for i in range(closure.n_polygons()):
        vs = closure.polygons[i].vertices
        block.area_steradians.values[i] = closure.polygons[i].area_steradians
        block.area_plate_carree.values[i] = polygon_area(vs[:, 0], vs[:, 1])

    # Assign block labels points to block interior points
    block["block_label"] = closure.assign_points(
        block.interior_lon.to_numpy(), block.interior_lat.to_numpy()
    )

    # I copied this from the bottom of:
    # https://stackoverflow.com/questions/39992502/rearrange-rows-of-pandas-dataframe-based-on-list-and-keeping-the-order
    # and I definitely don't understand it all but emperically it seems to work.
    block = (
        block.set_index(block.block_label, append=True)
        .sort_index(level=1)
        .reset_index(1, drop=True)
    )
    block = block.reset_index()
    block = block.loc[:, ~block.columns.str.match("index")]

    # Assign block labels to GPS stations
    if not station.empty:
        station["block_label"] = closure.assign_points(
            station.lon.to_numpy(), station.lat.to_numpy()
        )

    # Assign block labels to SAR locations
    if not sar.empty:
        sar["block_label"] = closure.assign_points(
            sar.lon.to_numpy(), sar.lat.to_numpy()
        )

    # Assign block labels to Mogi sources
    if not mogi.empty:
        mogi["block_label"] = closure.assign_points(
            mogi.lon.to_numpy(), mogi.lat.to_numpy()
        )

    return closure, block


def great_circle_latitude_find(lon1, lat1, lon2, lat2, lon):
    """
    Determines latitude as a function of longitude along a great circle.
    LAT = gclatfind(LON1, LAT1, LON2, LAT2, LON) finds the latitudes of points of
    specified LON that lie along the great circle defined by endpoints LON1, LAT1
    and LON2, LAT2. Angles should be passed as degrees.
    """
    lon1 = np.deg2rad(lon1)
    lat1 = np.deg2rad(lat1)
    lon2 = np.deg2rad(lon2)
    lat2 = np.deg2rad(lat2)
    lon = np.deg2rad(lon)
    lat = np.arctan(
        np.tan(lat1) * np.sin(lon - lon2) / np.sin(lon1 - lon2)
        - np.tan(lat2) * np.sin(lon - lon1) / np.sin(lon1 - lon2)
    )
    return lat


def process_sar(sar, command):
    """
    Preprocessing of SAR data.
    """
    if sar.empty:
        sar["depth"] = np.zeros_like(sar.lon)

        # Set the uncertainties to reflect the weights specified in the command file
        # In constructing the data weight vector, the value is 1./Sar.dataSig.^2, so
        # the adjustment made here is sar.dataSig / np.sqrt(command.sarWgt)
        sar.line_of_sight_change_sig = sar.line_of_sight_change_sig / np.sqrt(
            command.sar_weight
        )
        sar["x"], sar["y"], sar["z"] = sph2cart(sar.lon, sar.lat, RADIUS_EARTH)
        sar["block_label"] = -1 * np.ones_like(sar.x)
    else:
        sar["dep"] = []
        sar["x"] = []
        sar["y"] = []
        sar["x"] = []
        sar["block_label"] = []
    return sar


def merge_geodetic_data(assembly, station, sar):
    """
    Merge GPS and InSAR data to a single assembly object
    """
    assembly.data.n_stations = len(station)
    assembly.data.n_sar = len(sar)
    assembly.data.east_vel = station.east_vel.to_numpy()
    assembly.sigma.east_sig = station.east_sig.to_numpy()
    assembly.data.north_vel = station.north_vel.to_numpy()
    assembly.sigma.north_sig = station.north_sig.to_numpy()
    assembly.data.up_vel = station.up_vel.to_numpy()
    assembly.sigma.up_sig = station.up_sig.to_numpy()
    assembly.data.sar_line_of_sight_change_val = sar.line_of_sight_change_val.to_numpy()
    assembly.sigma.sar_line_of_sight_change_sig = (
        sar.line_of_sight_change_sig.to_numpy()
    )
    assembly.data.lon = np.concatenate((station.lon.to_numpy(), sar.lon.to_numpy()))
    assembly.data.lat = np.concatenate((station.lat.to_numpy(), sar.lat.to_numpy()))
    assembly.data.depth = np.concatenate(
        (station.depth.to_numpy(), sar.depth.to_numpy())
    )
    assembly.data.x = np.concatenate((station.x.to_numpy(), sar.x.to_numpy()))
    assembly.data.y = np.concatenate((station.y.to_numpy(), sar.y.to_numpy()))
    assembly.data.z = np.concatenate((station.z.to_numpy(), sar.z.to_numpy()))
    assembly.data.block_label = np.concatenate(
        (station.block_label.to_numpy(), sar.block_label.to_numpy())
    )
    assembly.index.sar_coordinate_idx = np.arange(
        len(station), len(station) + len(sar)
    )  # TODO: Not sure this is correct
    return assembly


def euler_pole_covariance_to_rotation_vector_covariance(
    omega_x, omega_y, omega_z, euler_pole_covariance_all
):
    """
    This function takes the model parameter covariance matrix
    in terms of the Euler pole and rotation rate and linearly
    propagates them to rotation vector space.
    """
    omega_x_sig = np.zeros_like(omega_x)
    omega_y_sig = np.zeros_like(omega_y)
    omega_z_sig = np.zeros_like(omega_z)
    for i in range(len(omega_x)):
        x = omega_x[i]
        y = omega_y[i]
        z = omega_z[i]
        euler_pole_covariance_current = euler_pole_covariance_all[
            3 * i : 3 * (i + 1), 3 * i : 3 * (i + 1)
        ]

        """
        There may be cases where x, y and z are all zero.  This leads to /0 errors.  To avoid this  %%
        we check for these cases and Let A = b * I where b is a small constant (10^-4) and I is     %%
        the identity matrix
        """
        if (x == 0) and (y == 0):
            euler_to_cartsian_operator = 1e-4 * np.eye(
                3
            )  # Set a default small value for rotation vector uncertainty
        else:
            # Calculate the partial derivatives
            dlat_dx = (
                -z / (x**2 + y**2) ** (3 / 2) / (1 + z**2 / (x**2 + y**2)) * x
            )
            dlat_dy = (
                -z / (x**2 + y**2) ** (3 / 2) / (1 + z**2 / (x**2 + y**2)) * y
            )
            dlat_dz = (
                1 / (x**2 + y**2) ** (1 / 2) / (1 + z**2 / (x**2 + y**2))
            )
            dlon_dx = -y / x**2 / (1 + (y / x) ** 2)
            dlon_dy = 1 / x / (1 + (y / x) ** 2)
            dlon_dz = 0
            dmag_dx = x / np.sqrt(x**2 + y**2 + z**2)
            dmag_dy = y / np.sqrt(x**2 + y**2 + z**2)
            dmag_dz = z / np.sqrt(x**2 + y**2 + z**2)
            euler_to_cartsian_operator = np.array(
                [
                    [dlat_dx, dlat_dy, dlat_dz],
                    [dlon_dx, dlon_dy, dlon_dz],
                    [dmag_dx, dmag_dy, dmag_dz],
                ]
            )

        # Propagate the Euler pole covariance matrix to a rotation rate
        # covariance matrix
        rotation_vector_covariance = (
            np.linalg.inv(euler_to_cartsian_operator)
            * euler_pole_covariance_current
            * np.linalg.inv(euler_to_cartsian_operator).T
        )

        # Organized data for the return
        main_diagonal_values = np.diag(rotation_vector_covariance)
        omega_x_sig[i] = np.sqrt(main_diagonal_values[0])
        omega_y_sig[i] = np.sqrt(main_diagonal_values[1])
        omega_z_sig[i] = np.sqrt(main_diagonal_values[2])
    return omega_x_sig, omega_y_sig, omega_z_sig


def get_block_motion_constraint_partials(block):
    """
    Partials for a priori block motion constraints.
    Essentially a set of eye(3) matrices
    """
    # apriori_block_idx = np.where(block.apriori_flag.to_numpy() == 1)[0]
    apriori_rotation_block_idx = np.where(block.rotation_flag.to_numpy() == 1)[0]

    operator = np.zeros((3 * len(apriori_rotation_block_idx), 3 * len(block)))
    for i in range(len(apriori_rotation_block_idx)):
        start_row = 3 * i
        start_column = 3 * apriori_rotation_block_idx[i]
        operator[start_row : start_row + 3, start_column : start_column + 3] = np.eye(3)
    return operator


def get_block_motion_constraints(assembly: Dict, block: pd.DataFrame, command: Dict):
    """
    Applying a priori block motion constraints
    """
    block_constraint_partials = get_block_motion_constraint_partials(block)
    assembly.index.block_constraints_idx = np.where(block.rotation_flag == 1)[0]

    assembly.data.n_block_constraints = len(assembly.index.block_constraints_idx)
    assembly.data.block_constraints = np.zeros(block_constraint_partials.shape[0])
    assembly.sigma.block_constraints = np.zeros(block_constraint_partials.shape[0])
    if assembly.data.n_block_constraints > 0:
        (
            assembly.data.block_constraints[0::3],
            assembly.data.block_constraints[1::3],
            assembly.data.block_constraints[2::3],
        ) = sph2cart(
            block.euler_lon[assembly.index.block_constraints_idx],
            block.euler_lat[assembly.index.block_constraints_idx],
            block.rotation_rate[assembly.index.block_constraints_idx],
        )
        euler_pole_covariance_all = np.diag(
            np.concatenate(
                (
                    np.deg2rad(
                        block.euler_lat_sig[assembly.index.block_constraints_idx]
                    ),
                    np.deg2rad(
                        block.euler_lon_sig[assembly.index.block_constraints_idx]
                    ),
                    np.deg2rad(
                        block.rotation_rate_sig[assembly.index.block_constraints_idx]
                    ),
                )
            )
        )
        (
            assembly.sigma.block_constraints[0::3],
            assembly.sigma.block_constraints[1::3],
            assembly.sigma.block_constraints[2::3],
        ) = euler_pole_covariance_to_rotation_vector_covariance(
            assembly.data.block_constraints[0::3],
            assembly.data.block_constraints[1::3],
            assembly.data.block_constraints[2::3],
            euler_pole_covariance_all,
        )
    assembly.sigma.block_constraint_weight = command.block_constraint_weight
    return assembly, block_constraint_partials


def get_cross_partials(vector):
    """
    Returns a linear operator R that when multiplied by
    vector a gives the cross product a cross b
    """
    return np.array(
        [
            [0, vector[2], -vector[1]],
            [-vector[2], 0, vector[0]],
            [vector[1], -vector[0], 0],
        ]
    )


def cartesian_vector_to_spherical_vector(vel_x, vel_y, vel_z, lon, lat):
    """
    This function transforms vectors from Cartesian to spherical components.
    Arguments:
        vel_x: array of x components of velocity
        vel_y: array of y components of velocity
        vel_z: array of z components of velocity
        lon: array of station longitudes
        lat: array of station latitudes
    Returned variables:
        vel_north: array of north components of velocity
        vel_east: array of east components of velocity
        vel_up: array of up components of velocity
    """
    projection_matrix = np.array(
        [
            [
                -np.sin(np.deg2rad(lat)) * np.cos(np.deg2rad(lon)),
                -np.sin(np.deg2rad(lat)) * np.sin(np.deg2rad(lon)),
                np.cos(np.deg2rad(lat)),
            ],
            [-np.sin(np.deg2rad(lon)), np.cos(np.deg2rad(lon)), 0],
            [
                -np.cos(np.deg2rad(lat)) * np.cos(np.deg2rad(lon)),
                -np.cos(np.deg2rad(lat)) * np.sin(np.deg2rad(lon)),
                -np.sin(np.deg2rad(lat)),
            ],
        ]
    )
    vel_north, vel_east, vel_up = np.dot(
        projection_matrix, np.array([vel_x, vel_y, vel_z])
    )
    return vel_north, vel_east, vel_up


def get_rotation_to_slip_rate_partials(segment, block):
    """
    Calculate partial derivatives relating relative block motion to fault slip rates
    """
    n_segments = len(segment)
    n_blocks = len(block)
    fault_slip_rate_partials = np.zeros((3 * n_segments, 3 * n_blocks))
    for i in range(n_segments):
        # Project velocities from Cartesian to spherical coordinates at segment mid-points
        row_idx = 3 * i
        column_idx_east = 3 * segment.east_labels[i]
        column_idx_west = 3 * segment.west_labels[i]
        R = get_cross_partials([segment.mid_x[i], segment.mid_y[i], segment.mid_z[i]])
        (
            vel_north_to_omega_x,
            vel_east_to_omega_x,
            _,
        ) = cartesian_vector_to_spherical_vector(
            R[0, 0], R[1, 0], R[2, 0], segment.mid_lon[i], segment.mid_lat[i]
        )
        (
            vel_north_to_omega_y,
            vel_east_to_omega_y,
            _,
        ) = cartesian_vector_to_spherical_vector(
            R[0, 1], R[1, 1], R[2, 1], segment.mid_lon[i], segment.mid_lat[i]
        )
        (
            vel_north_to_omega_z,
            vel_east_to_omega_z,
            _,
        ) = cartesian_vector_to_spherical_vector(
            R[0, 2], R[1, 2], R[2, 2], segment.mid_lon[i], segment.mid_lat[i]
        )

        # Build unit vector for the fault
        # Projection on to fault strike
        segment_azimuth, _, _ = GEOID.inv(
            segment.lon1[i], segment.lat1[i], segment.lon2[i], segment.lat2[i]
        )  # TODO: Need to check this vs. matlab azimuth for consistency
        unit_x_parallel = np.cos(np.deg2rad(90 - segment_azimuth))
        unit_y_parallel = np.sin(np.deg2rad(90 - segment_azimuth))
        unit_x_perpendicular = np.sin(np.deg2rad(segment_azimuth - 90))
        unit_y_perpendicular = np.cos(np.deg2rad(segment_azimuth - 90))

        # Projection onto fault dip
        if segment.lat2[i] < segment.lat1[i]:
            unit_x_parallel = -unit_x_parallel
            unit_y_parallel = -unit_y_parallel
            unit_x_perpendicular = -unit_x_perpendicular
            unit_y_perpendicular = -unit_y_perpendicular

        # This is the logic for dipping vs. non-dipping faults
        # If fault is dipping make it so that the dip slip rate has a fault normal
        # component equal to the fault normal differential plate velocity.  This
        # is kinematically consistent in the horizontal but *not* in the vertical.
        if segment.dip[i] != 90:
            scale_factor = 1 / abs(np.cos(np.deg2rad(segment.dip[i])))
            slip_rate_matrix = np.array(
                [
                    [
                        unit_x_parallel * vel_east_to_omega_x
                        + unit_y_parallel * vel_north_to_omega_x,
                        unit_x_parallel * vel_east_to_omega_y
                        + unit_y_parallel * vel_north_to_omega_y,
                        unit_x_parallel * vel_east_to_omega_z
                        + unit_y_parallel * vel_north_to_omega_z,
                    ],
                    [
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_x
                            + unit_y_perpendicular * vel_north_to_omega_x
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_y
                            + unit_y_perpendicular * vel_north_to_omega_y
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_z
                            + unit_y_perpendicular * vel_north_to_omega_z
                        ),
                    ],
                    [0, 0, 0],
                ]
            )
        else:
            scale_factor = (
                -1
            )  # This is for consistency with the Okada convention for tensile faulting
            slip_rate_matrix = np.array(
                [
                    [
                        unit_x_parallel * vel_east_to_omega_x
                        + unit_y_parallel * vel_north_to_omega_x,
                        unit_x_parallel * vel_east_to_omega_y
                        + unit_y_parallel * vel_north_to_omega_y,
                        unit_x_parallel * vel_east_to_omega_z
                        + unit_y_parallel * vel_north_to_omega_z,
                    ],
                    [0, 0, 0],
                    [
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_x
                            + unit_y_perpendicular * vel_north_to_omega_x
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_y
                            + unit_y_perpendicular * vel_north_to_omega_y
                        ),
                        scale_factor
                        * (
                            unit_x_perpendicular * vel_east_to_omega_z
                            + unit_y_perpendicular * vel_north_to_omega_z
                        ),
                    ],
                ]
            )

        fault_slip_rate_partials[
            row_idx : row_idx + 3, column_idx_east : column_idx_east + 3
        ] = slip_rate_matrix
        fault_slip_rate_partials[
            row_idx : row_idx + 3, column_idx_west : column_idx_west + 3
        ] = -slip_rate_matrix
    return fault_slip_rate_partials


def get_slip_rate_constraints(assembly, segment, block, command):
    # TODO: Revisit logging
    # logger.info("Isolating slip rate constraints")
    # for i in range(len(segment.lon1)):
    #     if segment.ss_rate_flag[i] == 1:
    #         # "{:.2f}".format(segment.ss_rate[i])
    #         logger.info(
    #             "Strike-slip rate constraint on "
    #             + segment.name[i].strip()
    #             + ": rate = "
    #             + "{:.2f}".format(segment.ss_rate[i])
    #             + " (mm/yr), 1-sigma uncertainty = +/-"
    #             + "{:.2f}".format(segment.ss_rate_sig[i])
    #             + " (mm/yr)"
    #         )
    #     if segment.ds_rate_flag[i] == 1:
    #         logger.info(
    #             "Dip-slip rate constraint on "
    #             + segment.name[i].strip()
    #             + ": rate = "
    #             + "{:.2f}".format(segment.ds_rate[i])
    #             + " (mm/yr), 1-sigma uncertainty = +/-"
    #             + "{:.2f}".format(segment.ds_rate_sig[i])
    #             + " (mm/yr)"
    #         )
    #     if segment.ts_rate_flag[i] == 1:
    #         logger.info(
    #             "Tensile-slip rate constraint on "
    #             + segment.name[i].strip()
    #             + ": rate = "
    #             + "{:.2f}".format(segment.ts_rate[i])
    #             + " (mm/yr), 1-sigma uncertainty = +/-"
    #             + "{:.2f}".format(segment.ts_rate_sig[i])
    #             + " (mm/yr)"
    #         )

    slip_rate_constraint_partials = get_rotation_to_slip_rate_partials(segment, block)

    slip_rate_constraint_flag = interleave3(
        segment.ss_rate_flag, segment.ds_rate_flag, segment.ts_rate_flag
    )
    assembly.index.slip_rate_constraints = np.where(slip_rate_constraint_flag == 1)[0]
    assembly.data.n_slip_rate_constraints = len(assembly.index.slip_rate_constraints)

    assembly.data.slip_rate_constraints = interleave3(
        segment.ss_rate, segment.ds_rate, segment.ts_rate
    )

    assembly.data.slip_rate_constraints = assembly.data.slip_rate_constraints[
        assembly.index.slip_rate_constraints
    ]

    assembly.sigma.slip_rate_constraints = interleave3(
        segment.ss_rate_sig, segment.ds_rate_sig, segment.ts_rate_sig
    )

    assembly.sigma.slip_rate_constraints = assembly.sigma.slip_rate_constraints[
        assembly.index.slip_rate_constraints
    ]

    slip_rate_constraint_partials = slip_rate_constraint_partials[
        assembly.index.slip_rate_constraints, :
    ]
    assembly.sigma.slip_rate_constraint_weight = command.slip_constraint_weight
    return assembly, slip_rate_constraint_partials


def get_segment_oblique_projection(lon1, lat1, lon2, lat2, skew=True):
    """
    Use pyproj oblique mercator: https://proj.org/operations/projections/omerc.html

    According to: https://proj.org/operations/projections/omerc.html
    This is this already rotated by the fault strike but the rotation can be undone with +no_rot
    > +no_rot
    > No rectification (not “no rotation” as one may well assume).
    > Do not take the last step from the skew uv-plane to the map XY plane.
    > Note: This option is probably only marginally useful,
    > but remains for (mostly) historical reasons.

    The version with north still pointing "up" appears to be called the
    Rectified skew orthomorphic projection or Hotine oblique Mercator projection
    https://pro.arcgis.com/en/pro-app/latest/help/mapping/properties/rectified-skew-orthomorphic.htm
    """
    if lon1 > 180.0:
        lon1 = lon1 - 360
    if lon2 > 180.0:
        lon2 = lon2 - 360
    projection_string = (
        "+proj=omerc "
        + "+lon_1="
        + str(lon1)
        + " "
        + "+lat_1="
        + str(lat1)
        + " "
        + "+lon_2="
        + str(lon2)
        + " "
        + "+lat_2="
        + str(lat2)
        + " "
        + "+ellps=WGS84"
    )
    if not skew:
        projection_string += " +no_rot"
    projection = pyproj.Proj(pyproj.CRS.from_proj4(projection_string))
    return projection


def get_okada_displacements(
    segment_lon1,
    segment_lat1,
    segment_lon2,
    segment_lat2,
    segment_locking_depth,
    segment_burial_depth,
    segment_dip,
    material_lambda,
    material_mu,
    strike_slip,
    dip_slip,
    tensile_slip,
    station_lon,
    station_lat,
):
    """
    Caculate elastic displacements in a homogeneous elastic half-space.
    Inputs are in geographic coordinates and then projected into a local
    xy-plane using a oblique Mercator projection that is tangent and parallel
    to the trace of the fault segment.  The elastic calculation is the
    original Okada 1992 Fortran code acceccesed through T. Ben Thompson's
    okada_wrapper: https://github.com/tbenthompson/okada_wrapper
    """
    segment_locking_depth *= KM2M
    segment_burial_depth *= KM2M

    # Project coordinates to flat space using a local oblique Mercator projection
    projection = get_segment_oblique_projection(
        segment_lon1, segment_lat1, segment_lon2, segment_lat2
    )
    station_x, station_y = projection(station_lon, station_lat)
    segment_x1, segment_y1 = projection(segment_lon1, segment_lat1)
    segment_x2, segment_y2 = projection(segment_lon2, segment_lat2)

    # Calculate geometric fault parameters
    segment_strike = np.arctan2(
        segment_y2 - segment_y1, segment_x2 - segment_x1
    )  # radians
    segment_length = np.sqrt(
        (segment_y2 - segment_y1) ** 2.0 + (segment_x2 - segment_x1) ** 2.0
    )
    segment_up_dip_width = (segment_locking_depth - segment_burial_depth) / np.sin(
        np.deg2rad(segment_dip)
    )

    # Translate stations and segment so that segment mid-point is at the origin
    segment_x_mid = (segment_x1 + segment_x2) / 2.0
    segment_y_mid = (segment_y1 + segment_y2) / 2.0
    station_x -= segment_x_mid
    station_y -= segment_y_mid
    segment_x1 -= segment_x_mid
    segment_x2 -= segment_x_mid
    segment_y1 -= segment_y_mid
    segment_y2 -= segment_y_mid

    # Unrotate coordinates to eliminate strike, segment will lie along y = 0
    rotation_matrix = np.array(
        [
            [np.cos(segment_strike), -np.sin(segment_strike)],
            [np.sin(segment_strike), np.cos(segment_strike)],
        ]
    )
    station_x_rotated, station_y_rotated = np.hsplit(
        np.einsum("ij,kj->ik", np.dstack((station_x, station_y))[0], rotation_matrix.T),
        2,
    )

    # Elastic displacements from Okada 1992
    alpha = (material_lambda + material_mu) / (material_lambda + 2 * material_mu)
    u_x = np.zeros_like(station_x)
    u_y = np.zeros_like(station_x)
    u_up = np.zeros_like(station_x)
    for i in range(len(station_x)):
        _, u, _ = okada_wrapper.dc3dwrapper(
            alpha,  # (lambda + mu) / (lambda + 2 * mu)
            [
                station_x_rotated[i],
                station_y_rotated[i],
                0,
            ],  # (meters) observation point
            segment_locking_depth,  # (meters) depth of the fault origin
            segment_dip,  # (degrees) the dip-angle of the rectangular dislocation surface
            [
                -segment_length / 2,
                segment_length / 2,
            ],  # (meters) the along-strike range of the surface (al1,al2 in the original)
            [
                0,
                segment_up_dip_width,
            ],  # (meters) along-dip range of the surface (aw1, aw2 in the original)
            [strike_slip, dip_slip, tensile_slip],
        )  # (meters) strike-slip, dip-slip, tensile-slip
        u_x[i] = u[0]
        u_y[i] = u[1]
        u_up[i] = u[2]

    # Un-rotate displacement to account for projected fault strike
    u_east, u_north = np.hsplit(
        np.einsum("ij,kj->ik", np.dstack((u_x, u_y))[0], rotation_matrix), 2
    )
    return u_east, u_north, u_up


def get_segment_station_operator_okada(segment, station, command):
    """
    Calculates the elastic displacement partial derivatives based on the Okada
    formulation, using the source and receiver geometries defined in
    dicitonaries segment and stations. Before calculating the partials for
    each segment, a local oblique Mercator project is done.

    The linear operator is structured as ():

                ss(segment1)  ds(segment1) ts(segment1) ... ss(segmentN) ds(segmentN) ts(segmentN)
    ve(station 1)
    vn(station 1)
    vu(station 1)
    .
    .
    .
    ve(station N)
    vn(station N)
    vu(station N)

    """
    if not station.empty:
        okada_segment_operator = np.ones((3 * len(station), 3 * len(segment)))
        # Loop through each segment and calculate displacements for each slip component
        for i in tqdm(
            range(len(segment)), desc="Calculating Okada partials for segments"
        ):
            (
                u_east_strike_slip,
                u_north_strike_slip,
                u_up_strike_slip,
            ) = get_okada_displacements(
                segment.lon1[i],
                segment.lat1[i],
                segment.lon2[i],
                segment.lat2[i],
                segment.locking_depth[i],
                segment.burial_depth[i],
                segment.dip[i],
                command.material_lambda,
                command.material_mu,
                1,
                0,
                0,
                station.lon,
                station.lat,
            )
            (
                u_east_dip_slip,
                u_north_dip_slip,
                u_up_dip_slip,
            ) = get_okada_displacements(
                segment.lon1[i],
                segment.lat1[i],
                segment.lon2[i],
                segment.lat2[i],
                segment.locking_depth[i],
                segment.burial_depth[i],
                segment.dip[i],
                command.material_lambda,
                command.material_mu,
                0,
                1,
                0,
                station.lon,
                station.lat,
            )
            (
                u_east_tensile_slip,
                u_north_tensile_slip,
                u_up_tensile_slip,
            ) = get_okada_displacements(
                segment.lon1[i],
                segment.lat1[i],
                segment.lon2[i],
                segment.lat2[i],
                segment.locking_depth[i],
                segment.burial_depth[i],
                segment.dip[i],
                command.material_lambda,
                command.material_mu,
                0,
                0,
                1,
                station.lon,
                station.lat,
            )
            segment_column_start_idx = 3 * i
            okada_segment_operator[0::3, segment_column_start_idx] = np.squeeze(
                u_east_strike_slip
            )
            okada_segment_operator[1::3, segment_column_start_idx] = np.squeeze(
                u_north_strike_slip
            )
            okada_segment_operator[2::3, segment_column_start_idx] = np.squeeze(
                u_up_strike_slip
            )
            okada_segment_operator[0::3, segment_column_start_idx + 1] = np.squeeze(
                u_east_dip_slip
            )
            okada_segment_operator[1::3, segment_column_start_idx + 1] = np.squeeze(
                u_north_dip_slip
            )
            okada_segment_operator[2::3, segment_column_start_idx + 1] = np.squeeze(
                u_up_dip_slip
            )
            okada_segment_operator[0::3, segment_column_start_idx + 2] = np.squeeze(
                u_east_tensile_slip
            )
            okada_segment_operator[1::3, segment_column_start_idx + 2] = np.squeeze(
                u_north_tensile_slip
            )
            okada_segment_operator[2::3, segment_column_start_idx + 2] = np.squeeze(
                u_up_tensile_slip
            )
    else:
        okada_segment_operator = np.empty(1)
    return okada_segment_operator


def get_elastic_operators(
    operators: Dict,
    meshes: List,
    segment: pd.DataFrame,
    station: pd.DataFrame,
    command: Dict,
):
    """
    Calculate (or load previously calculated) elastic operators from
    both fully locked segments and TDE parameterizes surfaces

    Args:
        operators (Dict): Elastic operators will be added to this data structure
        meshes (List): Geometries of meshes
        segment (pd.DataFrame): All segment data
        station (pd.DataFrame): All station data
        command (Dict): All command data
    """
    if (command.reuse_elastic == "yes") and (
        os.path.exists(command.reuse_elastic_file)
    ):
        # TODO: If it doesn't exist then calculate the partials
        print("Using precomputed elastic operators")
        hdf5_file = h5py.File(command.reuse_elastic_file, "r")

        operators.slip_rate_to_okada_to_velocities = np.array(
            hdf5_file.get("slip_rate_to_okada_to_velocities")
        )
        for i in range(len(meshes)):
            operators.tde_to_velocities[i] = np.array(
                hdf5_file.get("tde_to_velocities_" + str(i))
            )
        hdf5_file.close()

    else:
        if not os.path.exists(command.reuse_elastic_file):
            print("Precomputed elastic operator file not found")
        print("Computing elastic operators")

        # Calculate Okada partials for all segments
        operators.slip_rate_to_okada_to_velocities = get_segment_station_operator_okada(
            segment, station, command
        )

        for i in range(len(meshes)):
            operators.tde_to_velocities[i] = get_tde_to_velocities_single_mesh(
                meshes, station, command, mesh_idx=i
            )

        # Save elastic to velocity matrices
        if command.save_elastic == "yes":
            # Check to see if "data/operators" folder exists and if not create it
            if not os.path.exists(command.operators_folder):
                os.mkdir(command.operators_folder)

            print(
                "Saving elastic to velocity matrices to :" + command.save_elastic_file
            )  # TODO: Move to logging
            hdf5_file = h5py.File(command.save_elastic_file, "w")

            hdf5_file.create_dataset(
                "slip_rate_to_okada_to_velocities",
                data=operators.slip_rate_to_okada_to_velocities,
            )
            for i in range(len(meshes)):
                hdf5_file.create_dataset(
                    "tde_to_velocities_" + str(i),
                    data=operators.tde_to_velocities[i],
                )
            hdf5_file.close()


def station_row_keep(assembly):
    """
    Determines which station rows should be retained based on up velocities
    TODO: I do not understand this!!!
    TODO: The logic in the first conditional seems to indicate that if there are
    no vertical velocities as a part of the data then they should be eliminated.
    TODO: Perhaps it would be better to make this a flag in command???
    """
    if np.sum(np.abs(assembly.data.up_vel)) == 0:
        assembly.index.station_row_keep = np.setdiff1d(
            np.arange(0, assembly.index.sz_rotation[0]),
            np.arange(2, assembly.index.sz_rotation[0], 3),
        )
    else:
        assembly.index.station_row_keep = np.arange(0, assembly.index.sz_rotation[1])
    return assembly


def mogi_forward(mogi_lon, mogi_lat, mogi_depth, poissons_ratio, obs_lon, obs_lat):
    """
    Calculate displacements from a single Mogi source using
    equation 7.14 from "Earthquake and Volcano Deformation" by Paul Segall
    """
    u_east = np.zeros_like(obs_lon)
    u_north = np.zeros_like(obs_lon)
    u_up = np.zeros_like(obs_lon)
    for i in range(obs_lon.size):
        # Find angle between source and observation as well as distance betwen them
        source_to_obs_forward_azimuth, _, source_to_obs_distance = GEOID.inv(
            mogi_lon, mogi_lat, obs_lon[i], obs_lat[i]
        )

        # Mogi displacements in cylindrical coordinates
        u_up[i] = (
            (1 - poissons_ratio)
            / np.pi
            * mogi_depth
            / ((source_to_obs_distance**2.0 + mogi_depth**2) ** 1.5)
        )
        u_radial = (
            (1 - poissons_ratio)
            / np.pi
            * source_to_obs_distance
            / ((source_to_obs_distance**2 + mogi_depth**2.0) ** 1.5)
        )

        # Convert radial displacement to east and north components
        u_east[i] = u_radial * np.sin(np.deg2rad(source_to_obs_forward_azimuth))
        u_north[i] = u_radial * np.cos(np.deg2rad(source_to_obs_forward_azimuth))
    return u_east, u_north, u_up


def get_mogi_to_velocities_partials(mogi, station, command):
    """
    Mogi volume change to station displacment operator
    """
    if mogi.empty:
        mogi_operator = np.empty(0)
    else:
        poissons_ratio = command.material_mu / (
            2 * (command.material_lambda + command.material_mu)
        )
        mogi_operator = np.zeros((3 * len(station), len(mogi)))
        for i in range(len(mogi)):
            mogi_depth = KM2M * mogi.depth[i]
            u_east, u_north, u_up = mogi_forward(
                mogi.lon[i],
                mogi.lat[i],
                mogi_depth,
                poissons_ratio,
                station.lon,
                station.lat,
            )

            # Insert components into partials matrix
            mogi_operator[0::3, i] = u_east
            mogi_operator[1::3, i] = u_north
            mogi_operator[2::3, i] = u_up
    return mogi_operator


def latitude_to_colatitude(lat):
    """
    Convert from latitude to colatitude
    NOTE: Not sure why I need to treat the scalar case differently but I do.
    """
    if lat.size == 1:  # Deal with the scalar case
        if lat >= 0:
            lat = 90.0 - lat
        elif lat < 0:
            lat = -90.0 - lat
    else:  # Deal with the array case
        lat[np.where(lat >= 0)[0]] = 90.0 - lat[np.where(lat >= 0)[0]]
        lat[np.where(lat < 0)[0]] = -90.0 - lat[np.where(lat < 0)[0]]
    return lat


def get_block_centroid(segment, block_idx):
    """
    Calculate centroid of a block based on boundary polygon
    We take all block vertices (including duplicates) and estimate
    the centroid by taking the average of longitude and latitude
    weighted by the length of the segment that each vertex is
    attached to.
    """
    segments_with_block_idx = np.union1d(
        np.where(segment.west_labels == block_idx)[0],
        np.where(segment.east_labels == block_idx)[0],
    )
    lon0 = np.concatenate(
        (segment.lon1[segments_with_block_idx], segment.lon2[segments_with_block_idx])
    )
    lat0 = np.concatenate(
        (segment.lat1[segments_with_block_idx], segment.lat2[segments_with_block_idx])
    )
    lengths = np.concatenate(
        (
            segment.length[segments_with_block_idx],
            segment.length[segments_with_block_idx],
        )
    )
    block_centroid_lon = np.average(lon0, weights=lengths)
    block_centroid_lat = np.average(lat0, weights=lengths)
    return block_centroid_lon, block_centroid_lat


def get_strain_rate_displacements(
    lon_obs,
    lat_obs,
    centroid_lon,
    centroid_lat,
    strain_rate_lon_lon,
    strain_rate_lat_lat,
    strain_rate_lon_lat,
):
    """
    Calculate displacements due to three strain rate components.
    Equations are from Savage (2001) and expressed concisely in McCaffrey (2005)
    In McCaffrey (2005) these are the two unnumbered equations at the bottom
    of page 2
    """
    centroid_lon = np.deg2rad(centroid_lon)
    centroid_lat = latitude_to_colatitude(centroid_lat)
    centroid_lat = np.deg2rad(centroid_lat)
    lon_obs = np.deg2rad(lon_obs)
    lat_obs = latitude_to_colatitude(lat_obs)
    lat_obs = np.deg2rad(lat_obs)

    # Calculate displacements from homogeneous strain
    u_up = np.zeros(
        lon_obs.size
    )  # Always zero here because we're assuming plane strain on the sphere
    u_east = strain_rate_lon_lon * (
        RADIUS_EARTH * (lon_obs - centroid_lon) * np.sin(centroid_lat)
    ) + strain_rate_lon_lat * (RADIUS_EARTH * (lat_obs - centroid_lat))
    u_north = strain_rate_lon_lat * (
        RADIUS_EARTH * (lon_obs - centroid_lon) * np.sin(centroid_lat)
    ) + strain_rate_lat_lat * (RADIUS_EARTH * (lat_obs - centroid_lat))
    return u_east, u_north, u_up


def get_block_strain_rate_to_velocities_partials(block, station, segment):
    """
    Calculate strain partial derivatives assuming a strain centroid at the center of each block
    """
    strain_rate_block_idx = np.where(block.strain_rate_flag.to_numpy() > 0)[0]
    if strain_rate_block_idx.size > 0:
        block_strain_rate_operator = np.zeros(
            (3 * len(station), 3 * strain_rate_block_idx.size)
        )
        for i in range(strain_rate_block_idx.size):
            # Find centroid of current block
            block_centroid_lon, block_centroid_lat = get_block_centroid(
                segment, strain_rate_block_idx[i]
            )

            # Find stations on current block
            station_idx = np.where(station.block_label == strain_rate_block_idx[i])[0]
            stations_block_lon = station.lon[station_idx].to_numpy()
            stations_block_lat = station.lat[station_idx].to_numpy()

            # Calculate partials for each component of strain rate
            (
                vel_east_lon_lon,
                vel_north_lon_lon,
                vel_up_lon_lon,
            ) = get_strain_rate_displacements(
                stations_block_lon,
                stations_block_lat,
                block_centroid_lon,
                block_centroid_lat,
                strain_rate_lon_lon=1,
                strain_rate_lat_lat=0,
                strain_rate_lon_lat=0,
            )
            (
                vel_east_lat_lat,
                vel_north_lat_lat,
                vel_up_lat_lat,
            ) = get_strain_rate_displacements(
                stations_block_lon,
                stations_block_lat,
                block_centroid_lon,
                block_centroid_lat,
                strain_rate_lon_lon=0,
                strain_rate_lat_lat=1,
                strain_rate_lon_lat=0,
            )
            (
                vel_east_lon_lat,
                vel_north_lon_lat,
                vel_up_lon_lat,
            ) = get_strain_rate_displacements(
                stations_block_lon,
                stations_block_lat,
                block_centroid_lon,
                block_centroid_lat,
                strain_rate_lon_lon=0,
                strain_rate_lat_lat=0,
                strain_rate_lon_lat=1,
            )
            block_strain_rate_operator[3 * station_idx, 3 * i] = vel_east_lon_lon
            block_strain_rate_operator[3 * station_idx, 3 * i + 1] = vel_east_lat_lat
            block_strain_rate_operator[3 * station_idx, 3 * i + 2] = vel_east_lon_lat
            block_strain_rate_operator[3 * station_idx + 1, 3 * i] = vel_north_lon_lon
            block_strain_rate_operator[
                3 * station_idx + 1, 3 * i + 1
            ] = vel_north_lat_lat
            block_strain_rate_operator[
                3 * station_idx + 1, 3 * i + 2
            ] = vel_north_lon_lat
            block_strain_rate_operator[3 * station_idx + 2, 3 * i] = vel_up_lon_lon
            block_strain_rate_operator[3 * station_idx + 2, 3 * i + 1] = vel_up_lat_lat
            block_strain_rate_operator[3 * station_idx + 2, 3 * i + 2] = vel_up_lon_lat
    else:
        block_strain_rate_operator = np.empty(0)
    return block_strain_rate_operator, strain_rate_block_idx


def get_rotation_displacements(lon_obs, lat_obs, omega_x, omega_y, omega_z):
    """
    Get displacments at at longitude and latitude coordinates given rotation
    vector components (omega_x, omega_y, omega_z)
    """
    vel_east = np.zeros(lon_obs.size)
    vel_north = np.zeros(lon_obs.size)
    vel_up = np.zeros(lon_obs.size)
    x, y, z = sph2cart(lon_obs, lat_obs, RADIUS_EARTH)
    for i in range(lon_obs.size):
        cross_product_operator = get_cross_partials([x[i], y[i], z[i]])
        (
            vel_north_from_omega_x,
            vel_east_from_omega_x,
            vel_up_from_omega_x,
        ) = cartesian_vector_to_spherical_vector(
            cross_product_operator[0, 0],
            cross_product_operator[1, 0],
            cross_product_operator[2, 0],
            lon_obs[i],
            lat_obs[i],
        )
        (
            vel_north_from_omega_y,
            vel_east_from_omega_y,
            vel_up_from_omega_y,
        ) = cartesian_vector_to_spherical_vector(
            cross_product_operator[0, 1],
            cross_product_operator[1, 1],
            cross_product_operator[2, 1],
            lon_obs[i],
            lat_obs[i],
        )
        (
            vel_north_from_omega_z,
            vel_east_from_omega_z,
            vel_up_from_omega_z,
        ) = cartesian_vector_to_spherical_vector(
            cross_product_operator[0, 2],
            cross_product_operator[1, 2],
            cross_product_operator[2, 2],
            lon_obs[i],
            lat_obs[i],
        )
        vel_east[i] = (
            omega_x * vel_east_from_omega_x
            + omega_y * vel_east_from_omega_y
            + omega_z * vel_east_from_omega_z
        )
        vel_north[i] = (
            omega_x * vel_north_from_omega_x
            + omega_y * vel_north_from_omega_y
            + omega_z * vel_north_from_omega_z
        )
    return vel_east, vel_north, vel_up


def get_rotation_to_velocities_partials(station):
    """
    Calculate block rotation partials operator for stations in dataframe
    station.
    """
    n_blocks = (
        np.max(station.block_label.values) + 1
    )  # +1 required so that a single block with index zero still propagates
    block_rotation_operator = np.zeros((3 * len(station), 3 * n_blocks))
    for i in range(n_blocks):
        station_idx = np.where(station.block_label == i)[0]
        (
            vel_east_omega_x,
            vel_north_omega_x,
            vel_up_omega_x,
        ) = get_rotation_displacements(
            station.lon.values[station_idx],
            station.lat.values[station_idx],
            omega_x=1,
            omega_y=0,
            omega_z=0,
        )
        (
            vel_east_omega_y,
            vel_north_omega_y,
            vel_up_omega_y,
        ) = get_rotation_displacements(
            station.lon.values[station_idx],
            station.lat.values[station_idx],
            omega_x=0,
            omega_y=1,
            omega_z=0,
        )
        (
            vel_east_omega_z,
            vel_north_omega_z,
            vel_up_omega_z,
        ) = get_rotation_displacements(
            station.lon.values[station_idx],
            station.lat.values[station_idx],
            omega_x=0,
            omega_y=0,
            omega_z=1,
        )
        block_rotation_operator[3 * station_idx, 3 * i] = vel_east_omega_x
        block_rotation_operator[3 * station_idx, 3 * i + 1] = vel_east_omega_y
        block_rotation_operator[3 * station_idx, 3 * i + 2] = vel_east_omega_z
        block_rotation_operator[3 * station_idx + 1, 3 * i] = vel_north_omega_x
        block_rotation_operator[3 * station_idx + 1, 3 * i + 1] = vel_north_omega_y
        block_rotation_operator[3 * station_idx + 1, 3 * i + 2] = vel_north_omega_z
        block_rotation_operator[3 * station_idx + 2, 3 * i] = vel_up_omega_x
        block_rotation_operator[3 * station_idx + 2, 3 * i + 1] = vel_up_omega_y
        block_rotation_operator[3 * station_idx + 2, 3 * i + 2] = vel_up_omega_z
    return block_rotation_operator


def get_global_float_block_rotation_partials(station):
    """
    Return a linear operator for the rotations of all stations assuming they
    are the on the same block (i.e., the globe). The purpose of this is to
    allow all of the stations to "float" in the inverse problem reducing the
    dependence on reference frame specification. This is done by making a
    copy of the station data frame, setting "block_label" for all stations
    equal to zero and then calling the standard block rotation operator
    function. The matrix returned here only has 3 columns
    """
    station_all_on_one_block = station.copy()
    station_all_on_one_block.block_label.values[
        :
    ] = 0  # Force all stations to be on one block
    global_float_block_rotation_operator = get_rotation_to_velocities_partials(
        station_all_on_one_block
    )
    return global_float_block_rotation_operator


def plot_block_labels(segment, block, station, closure):
    plt.figure()
    plt.title("West and east labels")
    for i in range(closure.n_polygons()):
        plt.plot(
            closure.polygons[i].vertices[:, 0],
            closure.polygons[i].vertices[:, 1],
            "k-",
            linewidth=0.5,
        )

    for i in range(len(segment)):
        plt.text(
            segment.mid_lon_plate_carree.values[i],
            segment.mid_lat_plate_carree.values[i],
            str(segment["west_labels"][i]) + "," + str(segment["east_labels"][i]),
            fontsize=8,
            color="m",
            horizontalalignment="center",
            verticalalignment="center",
        )

    for i in range(len(station)):
        plt.text(
            station.lon.values[i],
            station.lat.values[i],
            str(station.block_label[i]),
            fontsize=8,
            color="k",
            horizontalalignment="center",
            verticalalignment="center",
        )

    for i in range(len(block)):
        plt.text(
            block.interior_lon.values[i],
            block.interior_lat.values[i],
            str(block.block_label[i]),
            fontsize=8,
            color="g",
            horizontalalignment="center",
            verticalalignment="center",
        )

    plt.gca().set_aspect("equal")
    plt.show()


def get_transverse_projection(lon0, lat0):
    """
    Use pyproj oblique mercator: https://proj.org/operations/projections/tmerc.html
    """
    if lon0 > 180.0:
        lon0 = lon0 - 360
    projection_string = (
        "+proj=tmerc "
        + "+lon_0="
        + str(lon0)
        + " "
        + "+lat_0="
        + str(lat0)
        + " "
        + "+ellps=WGS84"
    )
    projection = pyproj.Proj(pyproj.CRS.from_proj4(projection_string))
    return projection


def get_tri_displacements(
    obs_lon,
    obs_lat,
    meshes,
    material_lambda,
    material_mu,
    tri_idx,
    strike_slip,
    dip_slip,
    tensile_slip,
):
    """
    Calculate surface displacments due to slip on a triangular dislocation
    element in a half space.  Includes projection from longitude and
    latitude to locally tangent planar coordinate system.
    """
    poissons_ratio = material_mu / (2 * (material_mu + material_lambda))

    # Project coordinates
    tri_centroid_lon = meshes[0].centroids[tri_idx, 0]
    tri_centroid_lat = meshes[0].centroids[tri_idx, 1]
    projection = get_transverse_projection(tri_centroid_lon, tri_centroid_lat)
    obs_x, obs_y = projection(obs_lon, obs_lat)
    tri_x1, tri_y1 = projection(meshes[0].lon1[tri_idx], meshes[0].lat1[tri_idx])
    tri_x2, tri_y2 = projection(meshes[0].lon2[tri_idx], meshes[0].lat2[tri_idx])
    tri_x3, tri_y3 = projection(meshes[0].lon3[tri_idx], meshes[0].lat3[tri_idx])
    tri_z1 = KM2M * meshes[0].dep1[tri_idx]
    tri_z2 = KM2M * meshes[0].dep2[tri_idx]
    tri_z3 = KM2M * meshes[0].dep3[tri_idx]

    # Package coordinates for cutde call
    obs_coords = np.vstack((obs_x, obs_y, np.zeros_like(obs_x))).T
    tri_coords = np.array(
        [[tri_x1, tri_y1, tri_z1], [tri_x2, tri_y2, tri_z2], [tri_x3, tri_y3, tri_z3]]
    )

    # Call cutde, multiply by displacements, and package for the return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        disp_mat = cutde_halfspace.disp_matrix(
            obs_pts=obs_coords, tris=np.array([tri_coords]), nu=poissons_ratio
        )
    slip = np.array([[strike_slip, dip_slip, tensile_slip]])
    disp = disp_mat.reshape((-1, 3)).dot(slip.flatten())
    vel_east = disp[0::3]
    vel_north = disp[1::3]
    vel_up = disp[2::3]
    return vel_east, vel_north, vel_up


def get_tri_displacements_single_mesh(
    obs_lon,
    obs_lat,
    meshes,
    material_lambda,
    material_mu,
    tri_idx,
    strike_slip,
    dip_slip,
    tensile_slip,
    mesh_idx,
):
    """
    Calculate surface displacments due to slip on a triangular dislocation
    element in a half space.  Includes projection from longitude and
    latitude to locally tangent planar coordinate system.
    """
    poissons_ratio = material_mu / (2 * (material_mu + material_lambda))

    # Project coordinates
    tri_centroid_lon = meshes[mesh_idx].centroids[tri_idx, 0]
    tri_centroid_lat = meshes[mesh_idx].centroids[tri_idx, 1]
    projection = get_transverse_projection(tri_centroid_lon, tri_centroid_lat)
    obs_x, obs_y = projection(obs_lon, obs_lat)
    tri_x1, tri_y1 = projection(
        meshes[mesh_idx].lon1[tri_idx], meshes[mesh_idx].lat1[tri_idx]
    )
    tri_x2, tri_y2 = projection(
        meshes[mesh_idx].lon2[tri_idx], meshes[mesh_idx].lat2[tri_idx]
    )
    tri_x3, tri_y3 = projection(
        meshes[mesh_idx].lon3[tri_idx], meshes[mesh_idx].lat3[tri_idx]
    )
    tri_z1 = KM2M * meshes[mesh_idx].dep1[tri_idx]
    tri_z2 = KM2M * meshes[mesh_idx].dep2[tri_idx]
    tri_z3 = KM2M * meshes[mesh_idx].dep3[tri_idx]

    # Package coordinates for cutde call
    obs_coords = np.vstack((obs_x, obs_y, np.zeros_like(obs_x))).T
    tri_coords = np.array(
        [[tri_x1, tri_y1, tri_z1], [tri_x2, tri_y2, tri_z2], [tri_x3, tri_y3, tri_z3]]
    )

    # Call cutde, multiply by displacements, and package for the return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        disp_mat = cutde_halfspace.disp_matrix(
            obs_pts=obs_coords, tris=np.array([tri_coords]), nu=poissons_ratio
        )
    slip = np.array([[strike_slip, dip_slip, tensile_slip]])
    disp = disp_mat.reshape((-1, 3)).dot(slip.flatten())
    vel_east = disp[0::3]
    vel_north = disp[1::3]
    vel_up = disp[2::3]
    return vel_east, vel_north, vel_up


def get_tde_to_velocities(meshes, station, command):
    """
    Calculates the elastic displacement partial derivatives based on the
    T. Ben Thompson cutde of the Nikhool and Walters (2015) equations
    for the displacements resulting from slip on a triangular
    dislocation in a homogeneous elastic half space.

    The linear operator is structured as ():

                ss(tri1)  ds(tri1) ts(tri1) ... ss(triN) ds(triN) ts(triN)
    ve(station 1)
    vn(station 1)
    vu(station 1)
    .
    .
    .
    ve(station N)
    vn(station N)
    vu(station N)

    """
    if len(meshes) > 0:
        n_tris = meshes[0].lon1.size
        if not station.empty:
            tri_operator = np.zeros((3 * len(station), 3 * n_tris))

            # Loop through each segment and calculate displacements for each slip component
            for i in tqdm(
                range(n_tris), desc="Calculating cutde partials for triangles"
            ):
                # for i in tqdm(range(100), desc="Calculating cutde partials for triangles"):
                (
                    vel_east_strike_slip,
                    vel_north_strike_slip,
                    vel_up_strike_slip,
                ) = get_tri_displacements(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=1,
                    dip_slip=0,
                    tensile_slip=0,
                )
                (
                    vel_east_dip_slip,
                    vel_north_dip_slip,
                    vel_up_dip_slip,
                ) = get_tri_displacements(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=0,
                    dip_slip=1,
                    tensile_slip=0,
                )
                (
                    vel_east_tensile_slip,
                    vel_north_tensile_slip,
                    vel_up_tensile_slip,
                ) = get_tri_displacements(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=0,
                    dip_slip=0,
                    tensile_slip=1,
                )
                tri_operator[0::3, 3 * i] = np.squeeze(vel_east_strike_slip)
                tri_operator[1::3, 3 * i] = np.squeeze(vel_north_strike_slip)
                tri_operator[2::3, 3 * i] = np.squeeze(vel_up_strike_slip)
                tri_operator[0::3, 3 * i + 1] = np.squeeze(vel_east_dip_slip)
                tri_operator[1::3, 3 * i + 1] = np.squeeze(vel_north_dip_slip)
                tri_operator[2::3, 3 * i + 1] = np.squeeze(vel_up_dip_slip)
                tri_operator[0::3, 3 * i + 2] = np.squeeze(vel_east_tensile_slip)
                tri_operator[1::3, 3 * i + 2] = np.squeeze(vel_north_tensile_slip)
                tri_operator[2::3, 3 * i + 2] = np.squeeze(vel_up_tensile_slip)
        else:
            tri_operator = np.empty(0)
    else:
        tri_operator = np.empty(0)
    return tri_operator


def get_tde_to_velocities_single_mesh(meshes, station, command, mesh_idx):
    """
    Calculates the elastic displacement partial derivatives based on the
    T. Ben Thompson cutde of the Nikhool and Walters (2015) equations
    for the displacements resulting from slip on a triangular
    dislocation in a homogeneous elastic half space.

    The linear operator is structured as ():

                ss(tri1)  ds(tri1) ts(tri1) ... ss(triN) ds(triN) ts(triN)
    ve(station 1)
    vn(station 1)
    vu(station 1)
    .
    .
    .
    ve(station N)
    vn(station N)
    vu(station N)

    """
    if len(meshes) > 0:
        n_tris = meshes[mesh_idx].lon1.size
        if not station.empty:
            tri_operator = np.zeros((3 * len(station), 3 * n_tris))

            # Loop through each segment and calculate displacements for each slip component
            for i in tqdm(
                range(n_tris), desc="Calculating cutde partials for triangles"
            ):
                # for i in tqdm(range(100), desc="Calculating cutde partials for triangles"):
                (
                    vel_east_strike_slip,
                    vel_north_strike_slip,
                    vel_up_strike_slip,
                ) = get_tri_displacements_single_mesh(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=1,
                    dip_slip=0,
                    tensile_slip=0,
                    mesh_idx=mesh_idx,
                )
                (
                    vel_east_dip_slip,
                    vel_north_dip_slip,
                    vel_up_dip_slip,
                ) = get_tri_displacements_single_mesh(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=0,
                    dip_slip=1,
                    tensile_slip=0,
                    mesh_idx=mesh_idx,
                )
                (
                    vel_east_tensile_slip,
                    vel_north_tensile_slip,
                    vel_up_tensile_slip,
                ) = get_tri_displacements_single_mesh(
                    station.lon.to_numpy(),
                    station.lat.to_numpy(),
                    meshes,
                    command.material_lambda,
                    command.material_mu,
                    tri_idx=i,
                    strike_slip=0,
                    dip_slip=0,
                    tensile_slip=1,
                    mesh_idx=mesh_idx,
                )
                tri_operator[0::3, 3 * i] = np.squeeze(vel_east_strike_slip)
                tri_operator[1::3, 3 * i] = np.squeeze(vel_north_strike_slip)
                tri_operator[2::3, 3 * i] = np.squeeze(vel_up_strike_slip)
                tri_operator[0::3, 3 * i + 1] = np.squeeze(vel_east_dip_slip)
                tri_operator[1::3, 3 * i + 1] = np.squeeze(vel_north_dip_slip)
                tri_operator[2::3, 3 * i + 1] = np.squeeze(vel_up_dip_slip)
                tri_operator[0::3, 3 * i + 2] = np.squeeze(vel_east_tensile_slip)
                tri_operator[1::3, 3 * i + 2] = np.squeeze(vel_north_tensile_slip)
                tri_operator[2::3, 3 * i + 2] = np.squeeze(vel_up_tensile_slip)
        else:
            tri_operator = np.empty(0)
    else:
        tri_operator = np.empty(0)
    return tri_operator


def get_shared_sides(vertices):
    """
    Determine the indices of the triangular elements sharing
    one side with a particular element.
    Inputs:
    vertices: n x 3 array containing the 3 vertex indices of the n elements,
        assumes that values increase monotonically from 1:n

    Outputs:
    share: n x 3 array containing the indices of the m elements sharing a
        side with each of the n elements.  "-1" values in the array
        indicate elements with fewer than m neighbors (i.e., on
        the edge of the geometry).

    In general, elements will have 1 (mesh corners), 2 (mesh edges), or 3
    (mesh interiors) neighbors, but in the case of branching faults that
    have been adjusted with mergepatches, it's for edges and corners to
    also up to 3 neighbors.
    """
    # Make side arrays containing vertex indices of sides
    side_1 = np.sort(np.vstack((vertices[:, 0], vertices[:, 1])).T, 1)
    side_2 = np.sort(np.vstack((vertices[:, 1], vertices[:, 2])).T, 1)
    side_3 = np.sort(np.vstack((vertices[:, 0], vertices[:, 2])).T, 1)
    sides_all = np.vstack((side_1, side_2, side_3))

    # Find the unique sides - each side can part of at most 2 elements
    _, first_occurence_idx = np.unique(sides_all, return_index=True, axis=0)
    _, last_occurence_idx = np.unique(np.flipud(sides_all), return_index=True, axis=0)
    last_occurence_idx = sides_all.shape[0] - last_occurence_idx - 1

    # Shared sides are those whose first and last indices are not equal
    shared = np.where((last_occurence_idx - first_occurence_idx) != 0)[0]

    # These are the indices of the shared sides
    sside1 = first_occurence_idx[shared]  # What should I name these variables?
    sside2 = last_occurence_idx[shared]

    el1, sh1 = np.unravel_index(
        sside1, vertices.shape, order="F"
    )  # "F" is for fortran ordering.  What should I call this variables?
    el2, sh2 = np.unravel_index(sside2, vertices.shape, order="F")
    share = -1 * np.ones((vertices.shape[0], 3))
    for i in range(el1.size):
        share[el1[i], sh1[i]] = el2[i]
        share[el2[i], sh2[i]] = el1[i]
    share = share.astype(int)
    return share


def get_tri_shared_sides_distances(share, x_centroid, y_centroid, z_centroid):
    """
    Calculates the distances between the centroids of adjacent triangular
    elements, for use in smoothing algorithms.

    Inputs:
    share: n x 3 array output from ShareSides, containing the indices
        of up to 3 elements that share a side with each of the n elements.
    x_centroid: x coordinates of element centroids
    y_centroid: y coordinates of element centroids
    z_centroid: z coordinates of element centroids

    Outputs:
    dists: n x 3 array containing distance between each of the n elements
        and its 3 or fewer neighbors.  A distance of 0 does not imply
        collocated elements, but rather implies that there are fewer
        than 3 elements that share a side with the element in that row.
    """
    tri_shared_sides_distances = np.zeros(share.shape)
    for i in range(share.shape[0]):
        tri_shared_sides_distances[i, :] = np.sqrt(
            (x_centroid[i] - x_centroid[share[i, :]]) ** 2.0
            + (y_centroid[i] - y_centroid[share[i, :]]) ** 2.0
            + (z_centroid[i] - z_centroid[share[i, :]]) ** 2.0
        )
    tri_shared_sides_distances[np.where(share == -1)] = 0
    return tri_shared_sides_distances


def get_tri_smoothing_matrix(share, tri_shared_sides_distances):
    """
    Produces a smoothing matrix based on the scale-dependent
    umbrella operator (e.g., Desbrun et al., 1999; Resor, 2004).

    Inputs:
    share: n x 3 array of indices of the up to 3 elements sharing a side
        with each of the n elements
    tri_shared_sides_distances: n x 3 array of distances between each of the
        n elements and its up to 3 neighbors

    Outputs:
    smoothing matrix: 3n x 3n smoothing matrix
    """

    # Allocate sparse matrix for contructing smoothing matrix
    n_shared_tris = share.shape[0]
    smoothing_matrix = scipy.sparse.lil_matrix((3 * n_shared_tris, 3 * n_shared_tris))

    # Create a design matrix for Laplacian construction
    share_copy = copy.deepcopy(share)
    share_copy[np.where(share == -1)] = 0
    share_copy[np.where(share != -1)] = 1

    # Sum the distances between each element and its neighbors
    share_distances = np.sum(tri_shared_sides_distances, axis=1)
    leading_coefficient = 2.0 / share_distances

    # Replace zero distances with 1 to avoid divide by zero
    tri_shared_sides_distances[np.where(tri_shared_sides_distances == 0)] = 1

    # Take the reciprocal of the distances
    inverse_tri_shared_sides_distances = 1.0 / tri_shared_sides_distances

    # Diagonal terms # TODO: Defnitely not sure about his line!!!
    diagonal_terms = -leading_coefficient * np.sum(
        inverse_tri_shared_sides_distances * share_copy,
        axis=1,
    )

    # Off-diagonal terms
    off_diagonal_terms = (
        np.vstack((leading_coefficient, leading_coefficient, leading_coefficient)).T
        * inverse_tri_shared_sides_distances
        * share_copy
    )

    # Place the weights into the smoothing operator
    for j in range(3):
        for i in range(n_shared_tris):
            smoothing_matrix[3 * i + j, 3 * i + j] = diagonal_terms[i]
            if share[i, j] != -1:
                k = 3 * i + np.array([0, 1, 2])
                m = 3 * share[i, j] + np.array([0, 1, 2])
                smoothing_matrix[k, m] = off_diagonal_terms[i, j]
    return smoothing_matrix


def get_all_mesh_smoothing_matrices(meshes: List, operators: Dict):
    """
    Build smoothing matrices for each of the triangular meshes
    stored in meshes
    """
    for i in range(len(meshes)):
        # Get smoothing operator for a single mesh.
        meshes[i].share = get_shared_sides(meshes[i].verts)
        meshes[i].tri_shared_sides_distances = get_tri_shared_sides_distances(
            meshes[i].share,
            meshes[i].x_centroid,
            meshes[i].y_centroid,
            meshes[i].z_centroid,
        )
        operators.smoothing_matrix[i] = get_tri_smoothing_matrix(
            meshes[i].share, meshes[i].tri_shared_sides_distances
        )


def get_all_mesh_smoothing_matrices_simple(meshes: List, operators: Dict):
    """
    Build smoothing matrices for each of the triangular meshes
    stored in meshes
    These are the simple not distance weighted meshes
    """
    for i in range(len(meshes)):
        # Get smoothing operator for a single mesh.
        meshes[i].share = get_shared_sides(meshes[i].verts)
        meshes[i].tri_shared_sides_distances = get_tri_shared_sides_distances(
            meshes[i].share,
            meshes[i].x_centroid,
            meshes[i].y_centroid,
            meshes[i].z_centroid,
        )
        operators.smoothing_matrix_simple[i] = get_tri_smoothing_matrix_simple(
            meshes[i].share, N_MESH_DIM
        )


def get_tri_smoothing_matrix_simple(share, n_dim):
    """
    Produces a smoothing matrix based without scale-dependent
    weighting.

    Inputs:
    share: n x 3 array of indices of the up to 3 elements sharing a side
        with each of the n elements

    Outputs:
    smoothing matrix: n_dim * n x n_dim * n smoothing matrix
    """

    # Allocate sparse matrix for contructing smoothing matrix
    n_shared_tri = share.shape[0]
    smoothing_matrix = scipy.sparse.lil_matrix(
        (n_dim * n_shared_tri, n_dim * n_shared_tri)
    )

    for j in range(n_dim):
        for i in range(n_shared_tri):
            smoothing_matrix[n_dim * i + j, n_dim * i + j] = 3
            if share[i, j] != -1:
                k = n_dim * i + np.arange(n_dim)
                m = n_dim * share[i, j] + np.arange(n_dim)
                smoothing_matrix[k, m] = -1
    return smoothing_matrix


def get_ordered_edge_nodes(meshes: List):
    """Find exterior edges of each mesh and return them in the dictionary
    for each mesh.

    Args:
        meshes (List): list of mesh dictionaries
    """

    for i in range(len(meshes)):
        # Make side arrays containing vertex indices of sides
        vertices = meshes[i].verts
        side_1 = np.sort(np.vstack((vertices[:, 0], vertices[:, 1])).T, 1)
        side_2 = np.sort(np.vstack((vertices[:, 1], vertices[:, 2])).T, 1)
        side_3 = np.sort(np.vstack((vertices[:, 2], vertices[:, 0])).T, 1)
        all_sides = np.vstack((side_1, side_2, side_3))
        unique_sides, sides_count = np.unique(all_sides, return_counts=True, axis=0)
        edge_nodes = unique_sides[np.where(sides_count == 1)]

        meshes[i].ordered_edge_nodes = np.zeros_like(edge_nodes)
        meshes[i].ordered_edge_nodes[0, :] = edge_nodes[0, :]
        last_row = 0
        for j in range(1, len(edge_nodes)):
            idx = np.where(
                (edge_nodes == meshes[i].ordered_edge_nodes[j - 1, 1])
            )  # Edge node indices the same as previous row, second column
            next_idx = np.where(
                idx[0][:] != last_row
            )  # One of those indices is the last row itself. Find the other row index
            next_row = idx[0][next_idx]  # Index of the next ordered row
            next_col = idx[1][next_idx]  # Index of the next ordered column (1 or 2)
            if next_col == 1:
                next_col_ord = [1, 0]  # Flip edge ordering
            else:
                next_col_ord = [0, 1]
            meshes[i].ordered_edge_nodes[j, :] = edge_nodes[next_row, next_col_ord]
            last_row = (
                next_row  # Update last_row so that it's excluded in the next iteration
            )


def get_tde_slip_rate_constraints(meshes: List, operators: Dict):
    """Construct TDE slip rate constraint matrices for each mesh.
    These are essentially identity matrices, used to set TDE slip
    rates on elements lining the edges of the mesh, as controlled
    by input parameters
    top_slip_rate_constraint,
    bot_slip_rate_constraint,
    side_slip_rate_constraint

    Args:
        meshes (List): list of mesh dictionaries
        operators (Dict): dictionary of linear operators
    """
    for i in range(len(meshes)):
        # Empty constraint matrix
        tde_slip_rate_constraints = np.zeros((2 * meshes[i].n_tde, 2 * meshes[i].n_tde))
        # Top constraints
        if meshes[i].top_slip_rate_constraint > 0:
            # Indices of top elements
            top_indices = np.asarray(np.where(meshes[i].top_elements))
            # Indices of top elements' 2 slip components
            top_idx = get_2component_index(top_indices)
            tde_slip_rate_constraints[top_idx, top_idx] = 1
        # Bottom constraints
        if meshes[i].bot_slip_rate_constraint > 0:
            # Indices of bottom elements
            bot_indices = np.asarray(np.where(meshes[i].bot_elements))
            # Indices of bottom elements' 2 slip components
            bot_idx = get_2component_index(bot_indices)
            tde_slip_rate_constraints[bot_idx, bot_idx] = 1
        # Side constraints
        if meshes[i].side_slip_rate_constraint > 0:
            # Indices of side elements
            side_indices = np.asarray(np.where(meshes[i].side_elements))
            # Indices of side elements' 2 slip components
            side_idx = get_2component_index(side_indices)
            tde_slip_rate_constraints[side_idx, side_idx] = 1
        # Eliminate blank rows
        sum_constraint_columns = np.sum(tde_slip_rate_constraints, 1)
        tde_slip_rate_constraints = tde_slip_rate_constraints[
            sum_constraint_columns > 0, :
        ]
        operators.tde_slip_rate_constraints[i] = tde_slip_rate_constraints
        meshes[i].n_tde_constraints = np.sum(sum_constraint_columns > 0)


def get_keep_index_12(length_of_array: int):
    """Calculate an indexing array that given and array:
    [1, 2, 3, 4, 5, 6, 7, 8, 9]
    returns
    [1, 2, 4, 5, 7, 8]
    This is useful for selecting only indices associated with
    horizontal motions

    Args:
        length_of_array (int): Length of initial array.  Should be divisible by 3

    Returns:
        idx (np.array): Array of indices to return
    """
    idx = np.delete(np.arange(0, length_of_array), np.arange(2, length_of_array, 3))
    return idx


def interleave2(array_1, array_2):
    """Interleaves two arrays, with alternating entries.
    Given array_1 = [0, 2, 4, 6] and array_2 = [1, 3, 5, 7]
    returns
    [0, 1, 2, 3, 4, 5, 6, 7]
    This is useful for assembling velocity/slip components into a combined array.

    Args:
        array_1, array_2 (np.array): Arrays to interleave. Should be equal length

    Returns:
        interleaved_array (np.array): Interleaved array
    """
    interleaved_array = np.empty((array_1.size + array_2.size), dtype=array_1.dtype)
    interleaved_array[0::2] = array_1
    interleaved_array[1::2] = array_2
    return interleaved_array


def interleave3(array_1, array_2, array_3):
    """Interleaves three arrays, with alternating entries.
    Given array_1 = [0, 3, 6, 9], array_2 = [1, 4, 7, 10], and array_3 = [2, 5, 8, 11]
    returns
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    This is useful for assembling velocity/slip components into a combined array.

    Args:
        array_1, array_2, array_3 (np.array): Arrays to interleave. Should be equal length

    Returns:
        interleaved_array (np.array): Interleaved array
    """
    interleaved_array = np.empty(
        (array_1.size + array_2.size + array_3.size), dtype=array_1.dtype
    )
    interleaved_array[0::3] = array_1
    interleaved_array[1::3] = array_2
    interleaved_array[2::3] = array_3
    return interleaved_array


def get_2component_index(indices: np.array):
    """Returns indices into 2-component array, where each entry of input array
    corresponds to two entries in the 2-component array
    Given indices = [0, 2, 10, 6]
    returns
    [0, 1, 4, 5, 20, 21, 12, 13]
    This is useful for referencing velocity/slip components corresponding to a set
    of stations/faults.

    Args:
        indices (np.array): Element index array

    Returns:
        idx (np.array): Component index array (2 * length of indices)
    """
    idx = np.sort(np.append(2 * (indices + 1) - 2, 2 * (indices + 1) - 1))
    return idx


def get_3component_index(indices: np.array):
    """Returns indices into 3-component array, where each entry of input array
    corresponds to three entries in the 3-component array
    Given indices = [0, 2, 10, 6]
    returns
    [0, 1, 2, 6, 7, 8, 27, 28, 29, 15, 16, 17]
    This is useful for referencing velocity/slip components corresponding to a set
    of stations/faults.

    Args:
        indices (np.array): Element index array

    Returns:
        idx (np.array): Component index array (3 * length of indices)
    """
    idx = np.sort(
        np.append(3 * (indices + 1) - 3, (3 * (indices + 1) - 2, 3 * (indices + 1) - 1))
    )
    return idx


def post_process_estimation(
    estimation: Dict, operators: Dict, station: pd.DataFrame, index: Dict
):
    """Calculate derived values derived from the block model linear estimate (e.g., velocities, undertainties)

    Args:
        estimation (Dict): Estimated state vector and model covariance
        operators (Dict): All linear operators
        station (pd.DataFrame): GPS station data
        idx (Dict): Indices and counts of data and array sizes
    """

    estimation.predictions = estimation.operator @ estimation.state_vector
    estimation.vel = estimation.predictions[0 : 2 * index.n_stations]
    estimation.east_vel = estimation.vel[0::2]
    estimation.north_vel = estimation.vel[1::2]

    # Estimate slip rate uncertainties
    estimation.slip_rate_sigma = np.sqrt(
        np.diag(
            operators.rotation_to_slip_rate
            @ estimation.state_covariance_matrix[
                0 : 3 * index.n_blocks, 0 : 3 * index.n_blocks
            ]
            @ operators.rotation_to_slip_rate.T
        )
    )  # I don't think this is correct because for the case when there is a rotation vector a priori
    estimation.strike_slip_rate_sigma = estimation.slip_rate_sigma[0::3]
    estimation.dip_slip_rate_sigma = estimation.slip_rate_sigma[1::3]
    estimation.tensile_slip_rate_sigma = estimation.slip_rate_sigma[2::3]

    # Calculate mean squared residual velocity
    estimation.east_vel_residual = estimation.east_vel - station.east_vel
    estimation.north_vel_residual = estimation.north_vel - station.north_vel

    # Extract TDE slip rates from state vector
    estimation.tde_rates = estimation.state_vector[
        3 * index.n_blocks : 3 * index.n_blocks + 2 * index.n_tde_total
    ]
    estimation.tde_strike_slip_rates = estimation.tde_rates[0::2]
    estimation.tde_dip_slip_rates = estimation.tde_rates[1::2]

    # Extract segment slip rates from state vector
    estimation.slip_rates = (
        operators.rotation_to_slip_rate
        @ estimation.state_vector[0 : 3 * index.n_blocks]
    )
    estimation.strike_slip_rates = estimation.slip_rates[0::3]
    estimation.dip_slip_rates = estimation.slip_rates[1::3]
    estimation.tensile_slip_rates = estimation.slip_rates[2::3]

    # Calculate rotation only velocities
    estimation.vel_rotation = (
        operators.rotation_to_velocities[index.station_row_keep_index, :]
        @ estimation.state_vector[0 : 3 * index.n_blocks]
    )
    estimation.east_vel_rotation = estimation.vel_rotation[0::2]
    estimation.north_vel_rotation = estimation.vel_rotation[1::2]

    # Calculate fully locked segment velocities
    estimation.vel_elastic_segment = (
        operators.rotation_to_slip_rate_to_okada_to_velocities[
            index.station_row_keep_index, :
        ]
        @ estimation.state_vector[0 : 3 * index.n_blocks]
    )
    estimation.east_vel_elastic_segment = estimation.vel_elastic_segment[0::2]
    estimation.north_vel_elastic_segment = estimation.vel_elastic_segment[1::2]

    # TODO: Calculate block strain rate velocities
    estimation.east_vel_block_strain_rate = np.zeros(len(station))
    estimation.north_vel_block_strain_rate = np.zeros(len(station))

    # Calculate TDE velocities
    estimation.vel_tde = np.zeros(2 * index.n_stations)
    for i in range(len(operators.tde_to_velocities)):
        tde_keep_row_index = get_keep_index_12(operators.tde_to_velocities[i].shape[0])
        tde_keep_col_index = get_keep_index_12(operators.tde_to_velocities[i].shape[1])
        estimation.vel_tde += (
            operators.tde_to_velocities[i][tde_keep_row_index, :][:, tde_keep_col_index]
            @ estimation.state_vector[index.start_tde_col[i] : index.end_tde_col[i]]
        )
    estimation.east_vel_tde = estimation.vel_tde[0::2]
    estimation.north_vel_tde = estimation.vel_tde[1::2]


@pytest.mark.skip(reason="Writing output files")
def write_output(
    command: Dict,
    estimation: Dict,
    station: pd.DataFrame,
    segment: pd.DataFrame,
    block: pd.DataFrame,
    meshes: Dict,
):
    # Add model velocities to station dataframe and write .csv
    station["model_east_vel"] = estimation.east_vel
    station["model_north_vel"] = estimation.north_vel
    station["model_east_vel_residual"] = estimation.east_vel_residual
    station["model_east_vel_residual"] = estimation.north_vel_residual
    station["model_east_vel_rotation"] = estimation.east_vel_rotation
    station["model_north_vel_rotation"] = estimation.north_vel_rotation
    station["model_east_elastic_segment"] = estimation.east_vel_elastic_segment
    station["model_north_elastic_segment"] = estimation.north_vel_elastic_segment
    station["model_east_vel_tde"] = estimation.east_vel_tde
    station["model_north_vel_tde"] = estimation.north_vel_tde
    station["model_east_vel_block_strain_rate"] = estimation.east_vel_block_strain_rate
    station[
        "model_north_vel_block_strain_rate"
    ] = estimation.north_vel_block_strain_rate
    station_output_file_name = command.output_path + "/" + "model_station.csv"
    station.to_csv(station_output_file_name, index=False, float_format="%0.4f")

    # Add estimated slip rates to segment dataframe and write .csv
    segment["model_strike_slip_rate"] = estimation.strike_slip_rates
    segment["model_dip_slip_rate"] = estimation.dip_slip_rates
    segment["model_tensile_slip_rate"] = estimation.tensile_slip_rates
    segment["model_strike_slip_rate_uncertainty"] = estimation.strike_slip_rate_sigma
    segment["model_dip_slip_rate_uncertainty"] = estimation.strike_slip_rate_sigma
    segment["model_tensile_slip_rate_uncertainty"] = estimation.strike_slip_rate_sigma
    segment_output_file_name = command.output_path + "/" + "model_segment.csv"
    segment.to_csv(segment_output_file_name, index=False, float_format="%0.4f")

    # TODO: Add rotation rates and block strain rate block dataframe and write .csv

    # Construct mesh geometry dataframe
    mesh_outputs = pd.DataFrame()
    for i in range(len(meshes)):
        this_mesh_output = {
            "lon1": meshes[i].lon1,
            "lat1": meshes[i].lat1,
            "dep1": meshes[i].dep1,
            "lon2": meshes[i].lon2,
            "lat2": meshes[i].lat2,
            "dep2": meshes[i].dep2,
            "lon3": meshes[i].lon3,
            "lat3": meshes[i].lat3,
            "dep3": meshes[i].dep3,
        }
        this_mesh_output = pd.DataFrame(this_mesh_output)
        mesh_outputs = mesh_outputs.append(this_mesh_output)
    # Append slip rates
    mesh_outputs["strike_slip_rate"] = estimation.tde_strike_slip_rates
    mesh_outputs["dip_slip_rate"] = estimation.tde_dip_slip_rates
    # Write to CSV
    mesh_output_file_name = command.output_path + "/" + "model_meshes.csv"
    mesh_outputs.to_csv(mesh_output_file_name, index=False)

    # TODO: Revisit logging
    # Move .log file to output folder
    # source_file = command.run_name + ".log"
    # target_file = command.output_path + "/" + command.run_name + ".log"
    # os.rename(source_file, target_file)


def get_mesh_edge_elements(meshes: List):
    # Find indices of elements lining top, bottom, and sides of each mesh

    get_ordered_edge_nodes(meshes)

    for i in range(len(meshes)):
        coords = meshes[i].meshio_object.points
        vertices = meshes[i].verts

        # Arrays of all element side node pairs
        side_1 = np.sort(np.vstack((vertices[:, 0], vertices[:, 1])).T, 1)
        side_2 = np.sort(np.vstack((vertices[:, 1], vertices[:, 2])).T, 1)
        side_3 = np.sort(np.vstack((vertices[:, 2], vertices[:, 0])).T, 1)

        # Sort edge node array
        sorted_edge_nodes = np.sort(meshes[i].ordered_edge_nodes, 1)

        # Indices of element sides that are in edge node array
        side_1_in_edge, side_1_in_edge_idx = ismember(sorted_edge_nodes, side_1, "rows")
        side_2_in_edge, side_2_in_edge_idx = ismember(sorted_edge_nodes, side_2, "rows")
        side_3_in_edge, side_3_in_edge_idx = ismember(sorted_edge_nodes, side_3, "rows")

        # Depths of nodes
        side_1_depths = np.abs(
            coords[
                np.column_stack(
                    (side_1[side_1_in_edge_idx, :], vertices[side_1_in_edge_idx, 2])
                ),
                2,
            ]
        )
        side_2_depths = np.abs(
            coords[
                np.column_stack(
                    (side_2[side_2_in_edge_idx, :], vertices[side_2_in_edge_idx, 0])
                ),
                2,
            ]
        )
        side_3_depths = np.abs(
            coords[
                np.column_stack(
                    (side_3[side_3_in_edge_idx, :], vertices[side_3_in_edge_idx, 1])
                ),
                2,
            ]
        )
        # Top elements are those where the depth difference between the non-edge node
        # and the mean of the edge nodes is greater than the depth difference between
        # the edge nodes themselves
        top1 = (side_1_depths[:, 2] - np.mean(side_1_depths[:, 0:2], 1)) > (
            np.abs(side_1_depths[:, 0] - side_1_depths[:, 1])
        )
        top2 = (side_2_depths[:, 2] - np.mean(side_2_depths[:, 0:2], 1)) > (
            np.abs(side_2_depths[:, 0] - side_2_depths[:, 1])
        )
        top3 = (side_3_depths[:, 2] - np.mean(side_3_depths[:, 0:2], 1)) > (
            np.abs(side_3_depths[:, 0] - side_3_depths[:, 1])
        )
        tops = np.full(len(vertices), False, dtype=bool)
        tops[side_1_in_edge_idx[top1]] = True
        tops[side_2_in_edge_idx[top2]] = True
        tops[side_3_in_edge_idx[top3]] = True
        meshes[i].top_elements = tops

        # Bottom elements are those where the depth difference between the non-edge node
        # and the mean of the edge nodes is more negative than the depth difference between
        # the edge nodes themselves
        bot1 = side_1_depths[:, 2] - np.mean(side_1_depths[:, 0:2], 1) < -np.abs(
            side_1_depths[:, 0] - side_1_depths[:, 1]
        )
        bot2 = side_2_depths[:, 2] - np.mean(side_2_depths[:, 0:2], 1) < -np.abs(
            side_2_depths[:, 0] - side_2_depths[:, 1]
        )
        bot3 = side_3_depths[:, 2] - np.mean(side_3_depths[:, 0:2], 1) < -np.abs(
            side_3_depths[:, 0] - side_3_depths[:, 1]
        )
        bots = np.full(len(vertices), False, dtype=bool)
        bots[side_1_in_edge_idx[bot1]] = True
        bots[side_2_in_edge_idx[bot2]] = True
        bots[side_3_in_edge_idx[bot3]] = True
        meshes[i].bot_elements = bots

        # Side elements are a set difference between all edges and tops, bottoms
        sides = np.full(len(vertices), False, dtype=bool)
        sides[side_1_in_edge_idx] = True
        sides[side_2_in_edge_idx] = True
        sides[side_3_in_edge_idx] = True
        sides[np.where(tops != 0)] = False
        sides[np.where(bots != 0)] = False
        meshes[i].side_elements = sides


def assemble_and_solve_dense(command, assembly, operators, station, block, meshes):
    # Create dictionary to store indices and sizes for operator building
    index = addict.Dict()
    index.n_stations = assembly.data.n_stations
    index.vertical_velocities = np.arange(2, 3 * index.n_stations, 3)
    index.n_blocks = len(block)
    index.n_block_constraints = assembly.data.n_block_constraints
    index.station_row_keep_index = get_keep_index_12(3 * len(station))
    index.start_station_row = 0
    index.end_station_row = 2 * len(station)
    index.start_block_col = 0
    index.end_block_col = 3 * len(block)
    index.start_block_constraints_row = index.end_station_row
    index.end_block_constraints_row = (
        index.start_block_constraints_row + 3 * index.n_block_constraints
    )
    index.n_slip_rate_constraints = assembly.data.slip_rate_constraints.size
    index.start_slip_rate_constraints_row = index.end_block_constraints_row
    index.end_slip_rate_constraints_row = (
        index.start_slip_rate_constraints_row + index.n_slip_rate_constraints
    )

    index.n_tde_total = 0
    index.n_tde_constraints_total = 0
    for i in range(len(meshes)):
        index.n_tde[i] = meshes[i].n_tde
        index.n_tde_total += index.n_tde[i]
        index.n_tde_constraints[i] = meshes[i].n_tde_constraints
        index.n_tde_constraints_total += index.n_tde_constraints[i]
        if i == 0:
            index.start_tde_col[i] = index.end_block_col
            index.end_tde_col[i] = index.start_tde_col[i] + 2 * index.n_tde[i]
            index.start_tde_smoothing_row[i] = index.end_slip_rate_constraints_row
            index.end_tde_smoothing_row[i] = (
                index.start_tde_smoothing_row[i] + 2 * index.n_tde[i]
            )
            index.start_tde_constraint_row[i] = index.end_tde_smoothing_row[i]
            index.end_tde_constraint_row[i] = (
                index.start_tde_constraint_row[i] + index.n_tde_constraints[i]
            )
        else:
            index.start_tde_col[i] = index.end_tde_col[i - 1]
            index.end_tde_col[i] = index.start_tde_col[i] + 2 * index.n_tde[i]
            index.start_tde_smoothing_row[i] = index.end_tde_constraint_row[i - 1]
            index.end_tde_smoothing_row[i] = (
                index.start_tde_smoothing_row[i] + 2 * index.n_tde[i]
            )
            index.start_tde_constraint_row[i] = index.end_tde_smoothing_row[i]
            index.end_tde_constraint_row[i] = (
                index.start_tde_constraint_row[i] + index.n_tde_constraints[i]
            )

    # Initialize data vector
    estimation = addict.Dict()
    estimation.data_vector = np.zeros(
        2 * index.n_stations
        + 3 * index.n_block_constraints
        + index.n_slip_rate_constraints
        + 2 * index.n_tde_total
        + index.n_tde_constraints_total
    )

    # Add GPS stations to data vector
    estimation.data_vector[
        index.start_station_row : index.end_station_row
    ] = interleave2(assembly.data.east_vel, assembly.data.north_vel)

    # Add block motion constraints to data vector
    estimation.data_vector[
        index.start_block_constraints_row : index.end_block_constraints_row
    ] = (DEG_PER_MYR_TO_RAD_PER_YR * assembly.data.block_constraints)

    # Add slip rate constraints to data vector
    estimation.data_vector[
        index.start_slip_rate_constraints_row : index.end_slip_rate_constraints_row
    ] = assembly.data.slip_rate_constraints

    # Initialize and build weighting matrix
    estimation.weighting_vector = np.ones(
        2 * index.n_stations
        + 3 * index.n_block_constraints
        + index.n_slip_rate_constraints
        + 2 * index.n_tde_total
        + index.n_tde_constraints_total
    )
    estimation.weighting_vector[
        index.start_station_row : index.end_station_row
    ] = interleave2(1 / (station.east_sig**2), 1 / (station.north_sig**2))
    estimation.weighting_vector[
        index.start_block_constraints_row : index.end_block_constraints_row
    ] = 1.0
    estimation.weighting_vector[
        index.start_slip_rate_constraints_row : index.end_slip_rate_constraints_row
    ] = command.slip_constraint_weight * np.ones(index.n_slip_rate_constraints)

    # Initialize linear operator
    estimation.operator = np.zeros(
        (
            2 * index.n_stations
            + 3 * index.n_block_constraints
            + index.n_slip_rate_constraints
            + 2 * index.n_tde_total
            + index.n_tde_constraints_total,
            3 * index.n_blocks + 2 * index.n_tde_total,
        )
    )

    # Insert block rotations and elastic velocities from fully locked segments
    operators.rotation_to_slip_rate_to_okada_to_velocities = (
        operators.slip_rate_to_okada_to_velocities @ operators.rotation_to_slip_rate
    )
    estimation.operator[
        index.start_station_row : index.end_station_row,
        index.start_block_col : index.end_block_col,
    ] = (
        operators.rotation_to_velocities[index.station_row_keep_index, :]
        - operators.rotation_to_slip_rate_to_okada_to_velocities[
            index.station_row_keep_index, :
        ]
    )

    # Insert block motion constraints
    estimation.operator[
        index.start_block_constraints_row : index.end_block_constraints_row,
        index.start_block_col : index.end_block_col,
    ] = operators.block_motion_constraints

    # Insert slip rate constraints
    estimation.operator[
        index.start_slip_rate_constraints_row : index.end_slip_rate_constraints_row,
        index.start_block_col : index.end_block_col,
    ] = operators.slip_rate_constraints

    # Insert TDE to velocity matrix
    for i in range(len(meshes)):
        # Insert TDE to velocity matrix
        tde_keep_row_index = get_keep_index_12(operators.tde_to_velocities[i].shape[0])
        tde_keep_col_index = get_keep_index_12(operators.tde_to_velocities[i].shape[1])
        estimation.operator[
            index.start_station_row : index.end_station_row,
            index.start_tde_col[i] : index.end_tde_col[i],
        ] = -operators.tde_to_velocities[i][tde_keep_row_index, :][
            :, tde_keep_col_index
        ]

        # Insert TDE smoothing matrix
        smoothing_keep_index = get_keep_index_12(
            operators.tde_to_velocities[i].shape[1]
        )
        estimation.operator[
            index.start_tde_smoothing_row[i] : index.end_tde_smoothing_row[i],
            index.start_tde_col[i] : index.end_tde_col[i],
        ] = operators.smoothing_matrix_simple[i].toarray()[smoothing_keep_index, :][
            :, smoothing_keep_index
        ]

        # Insert smoothing weight into weighting vector
        estimation.weighting_vector[
            index.start_tde_smoothing_row[i] : index.end_tde_smoothing_row[i]
        ] = meshes[i].smoothing_weight * np.ones(2 * index.n_tde[i])
        # print(estimation.weighting_vector[index.start_tde_smoothing_row[i]])
        # Insert TDE slip rate constraints into estimation operator
        estimation.operator[
            index.start_tde_constraint_row[i] : index.end_tde_constraint_row[i],
            index.start_tde_col[i] : index.end_tde_col[i],
        ] = operators.tde_slip_rate_constraints[i]

    # Solve the overdetermined linear system using only a weighting vector rather than matrix
    estimation.state_covariance_matrix = np.linalg.inv(
        estimation.operator.T * estimation.weighting_vector @ estimation.operator
    )
    estimation.state_vector = (
        estimation.state_covariance_matrix
        @ estimation.operator.T
        * estimation.weighting_vector
        @ estimation.data_vector
    )
    return index, estimation
