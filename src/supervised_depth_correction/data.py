from __future__ import absolute_import, division, print_function
import os
import glob
import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial.transform import Rotation
from scipy.spatial.transform import Slerp
from tqdm import tqdm
import datetime
import torch
from PIL import Image


# TODO: use os and file path instead
DATA_DIR = "/home/ruslan/data/datasets/kitti_raw"
# DATA_DIR = "/home/jachym/KITTI/kitti_raw"
# DEPTH_DATA_DIR = "/home/jachym/KITTI/depth_selection/val_selection_cropped"
DEPTH_DATA_DIR = "/home/ruslan/data/datasets/kitti_depth/depth_selection/val_selection_cropped"

sequence_names = [
    '2011_09_26',
    '2011_09_28',
    '2011_09_29',
    '2011_09_30',
    '2011_10_03'
]


class KITTIRawPoses(object):

    def __init__(self, subseq, path=None):
        if path is None:
            seq = subseq[:10]
            path = os.path.join(DATA_DIR, seq)
        self.path = path
        self.subseq = subseq
        self.gps2cam_transform = self.get_calibrations()
        self.poses = self.get_cam_poses()
        self.ts = self.get_timestamps(sensor='gps')
        self.ids = range(len(self.ts))

    @staticmethod
    def gps_to_ecef(lat, lon, alt, zero_origin=False):
        # https://gis.stackexchange.com/questions/230160/converting-wgs84-to-ecef-in-python
        rad_lat = lat * (np.pi / 180.0)
        rad_lon = lon * (np.pi / 180.0)

        a = 6378137.0
        finv = 298.257223563
        f = 1 / finv
        e2 = 1 - (1 - f) * (1 - f)
        v = a / np.sqrt(1 - e2 * np.sin(rad_lat) * np.sin(rad_lat))

        x = (v + alt) * np.cos(rad_lat) * np.cos(rad_lon)
        y = (v + alt) * np.cos(rad_lat) * np.sin(rad_lon)
        # TODO: check why height from latitude is too high
        # z = (v * (1 - e2) + alt) * np.sin(rad_lat)
        z = alt

        if zero_origin:
            x, y, z = x - x[0], y - y[0], z - z[0]
        return x, y, z

    def get_gps_pose(self, fname):
        assert isinstance(fname, str)
        gps_data = np.genfromtxt(fname)
        lat, lon, alt = gps_data[:3]
        roll, pitch, yaw = gps_data[3:6]
        R = Rotation.from_euler('xyz', [roll, pitch, yaw], degrees=False).as_matrix()
        x, y, z = self.gps_to_ecef(lat, lon, alt)
        # convert to 4x4 matrix
        pose = np.eye(4)
        pose[:3, :3] = R
        pose[:3, 3] = np.array([x, y, z])
        return pose

    def get_cam_poses(self, zero_origin=False):
        poses = []
        for fname in np.sort(glob.glob(os.path.join(self.path, self.subseq, 'oxts/data/*.txt'))):
            pose = self.get_gps_pose(fname)
            poses.append(pose)
        poses = np.asarray(poses)

        # TODO: now we have Tr(gps -> cam0), we need Tr(gps->cam2 or cam3)
        poses = np.matmul(poses, self.gps2cam_transform[None])

        if zero_origin:
            # move poses to 0 origin:
            Tr_inv = np.linalg.inv(poses[0])
            poses = np.asarray([np.matmul(Tr_inv, pose) for pose in poses])

        return poses

    def get_calibrations(self):
        # Load calibration matrices
        TrImuToVelo = self.load_calib_file("calib_imu_to_velo.txt")
        TrVeloToCam = self.load_calib_file("calib_velo_to_cam.txt")
        TrImuToCam0 = np.matmul(TrImuToVelo, TrVeloToCam)
        return np.asarray(TrImuToCam0)

    def load_calib_file(self, file):
        """Read calibration from file.
            :param str file: File name.
            :return numpy.matrix: Calibration.
            """
        fpath = os.path.join(self.path, file)
        with open(fpath, 'r') as f:
            s = f.read()
        i_r = s.index('R:')
        i_t = s.index('T:')
        i_t_end = i_t+2 + s[i_t+2:].index('\n')
        rotation = np.mat(s[i_r + 2:i_t], dtype=np.float64).reshape((3, 3))
        translation = np.mat(s[i_t + 2:i_t_end], dtype=np.float64).reshape((3, 1))
        transform = np.bmat([[rotation, translation], [[[0, 0, 0, 1]]]])
        assert transform.shape == (4, 4)
        return transform

    def get_timestamps(self, sensor='gps', zero_origin=False):
        assert isinstance(sensor, str)
        assert sensor == 'gps' or sensor == 'lidar'
        if sensor == 'gps':
            sensor_folder = 'oxts'
        elif sensor == 'lidar':
            sensor_folder = 'velodyne_points'
        else:
            raise ValueError
        timestamps = []
        ts = np.genfromtxt(os.path.join(self.path, self.subseq, sensor_folder, 'timestamps.txt'), dtype=str)
        for t in ts:
            date = t[0]
            day_time, sec = t[1].split(".")
            sec = float('0.' + sec)
            stamp = datetime.datetime.strptime("%s_%s" % (date, day_time), "%Y-%m-%d_%H:%M:%S").timestamp() + sec
            timestamps.append(stamp)
        if zero_origin:
            timestamps = [t - timestamps[0] for t in timestamps]
        return timestamps

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, i):
        assert i in self.ids
        return self.poses[i]

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]


class KITTIDepthSelection:

    """
    loads depth images, rgb images and intrinsics from
    KITTI depth: http://www.cvlibs.net/datasets/kitti/eval_depth.php?benchmark=depth_completion
    """

    def __init__(self, subseq, path=None, camera="left"):
        # path directory should contain folders: depth, rgb, intrinsics
        if path is None:
            path = DEPTH_DATA_DIR
        self.path = path
        self.subseq = subseq
        self.image = "image_02" if camera == 'left' else "image_03"
        self.ids = self.get_ids()

    def get_rgb(self, i):
        file = os.path.join(self.path, "image", "%s_image_%010d_%s.png" % (self.subseq, i, self.image))
        rgb = np.asarray(Image.open(file))
        return rgb

    def get_depth(self, i, gt=True, to_depth_map=False):
        """
        Depth maps (annotated and raw Velodyne scans) are saved as uint16 PNG images,
        which can be opened with either MATLAB, libpng++ or the latest version of
        Python's pillow (from PIL import Image). A 0 value indicates an invalid pixel
        (ie, no ground truth exists, or the estimation algorithm didn't produce an
        estimate for that pixel). Otherwise, the depth for a pixel can be computed
        in meters by converting the uint16 value to float and dividing it by 256.0:
        disp(u,v)  = ((float)I(u,v))/256.0;
        valid(u,v) = I(u,v)>0;
        """
        depth_label = "groundtruth_depth" if gt else "velodyne_raw"
        file = os.path.join(self.path, depth_label, "%s_%s_%010d_%s.png" % (self.subseq, depth_label, i, self.image))
        depth = np.array(Image.open(file), dtype=int)
        r, c = depth.shape[:2]
        # make sure we have a proper 16bit depth map here.. not 8bit!
        assert (np.max(depth) > 255)
        if to_depth_map:
            depth = depth.astype(np.float) / 256.
            depth[depth == 0] = -1.
        return depth.reshape([r, c, 1])

    def get_intrinsics(self, i):
        file = os.path.join(self.path, "intrinsics", "%s_image_%010d_%s.txt" % (self.subseq, i, self.image))
        K = np.loadtxt(file).reshape(3, 3)
        return K

    def get_ids(self, gt=True):
        ids = list()
        depth_label = "groundtruth_depth" if gt else "velodyne_raw"
        depth_files = sorted(glob.glob(os.path.join(self.path, depth_label,
                                                    "%s_%s_*_%s.png" % (self.subseq, depth_label, self.image))))
        for depth_file in depth_files:
            id = int(depth_file[-23:-13])
            ids.append(id)
        return ids

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, i):
        assert i in self.ids
        intrins = self.get_intrinsics(i)
        rgb = self.get_rgb(i)
        depth = self.get_depth(i)
        return rgb, depth, intrins


class Dataset:
    def __init__(self, subseq):
        self.ds_poses = KITTIRawPoses(subseq=subseq)
        self.ds_depths = KITTIDepthSelection(subseq=subseq)
        self.poses = self.ds_poses.poses[self.ds_depths.ids]
        self.ids = self.ds_depths.ids

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, item):
        """
        Provides input data, that could be used with GradSLAM
        Args:
            item: int

        Returns:
            data: list(colors, depths, intrinsics, poses)
                  colors: torch.Tensor (B x N x W x H x Crgb)
                  depths: torch.Tensor (B x N x W x H x Cd)
                  intrinsics: torch.Tensor (B x N x 4 x 4)
                  poses: torch.Tensor (B x N x 4 x 4)
        """
        assert item in self.ids
        colors, depths, K = self.ds_depths[item]
        poses = self.ds_poses[item]

        intrinsics = np.eye(4)
        intrinsics[:3, :3] = K

        data = [colors, depths, intrinsics, poses]
        data = [torch.as_tensor(d[None][None], dtype=torch.float32) for d in data]
        return data


def poses_demo():
    np.random.seed(135)
    seq = np.random.choice(sequence_names)
    while True:
        subseq = np.random.choice(os.listdir(os.path.join(DATA_DIR, seq)))
        if '2011_' in subseq:
            break
    # subseq = "2011_09_26_drive_0002_sync"

    ds = KITTIRawPoses(subseq=subseq)
    xs, ys, zs = ds.poses[:, 0, 3], ds.poses[:, 1, 3], ds.poses[:, 2, 3]

    plt.figure()
    plt.title("%s" % subseq)
    # plt.subplot(1, 2, 1)
    plt.plot(xs, ys, '.')
    plt.grid()
    plt.xlabel('X [m]')
    plt.ylabel('Y [m]')
    plt.axis('equal')

    # plt.subplot(1, 2, 2)
    # plt.xlabel('time [sec]')
    # plt.ylabel('Z [m]')
    # plt.plot(ds.ts, zs, '.')
    # plt.grid()
    # plt.axis('equal')
    plt.show()


def ts_demo():
    np.random.seed(135)
    seq = np.random.choice(sequence_names)
    while True:
        subseq = np.random.choice(os.listdir(os.path.join(DATA_DIR, seq)))
        if '2011_' in subseq:
            break

    ds = KITTIRawPoses(subseq=subseq)

    ts_gps = ds.get_timestamps(sensor='gps', zero_origin=True)
    ts_velo = ds.get_timestamps(sensor='lidar', zero_origin=True)

    plt.figure()
    plt.title("%s" % subseq)
    plt.plot(ts_gps[::5], '.', label='gps')
    plt.plot(ts_velo[::5], '.', label='lidar')
    plt.legend()
    plt.grid()
    plt.show()


def gradslam_demo():
    from gradslam import Pointclouds, RGBDImages
    from gradslam.slam import PointFusion
    import open3d as o3d

    # constructs global map using gradslam, visualizes resulting pointcloud
    subseq = "2011_09_26_drive_0002_sync"
    # subseq = "2011_09_26_drive_0005_sync"
    # subseq = "2011_09_26_drive_0023_sync"

    ds = Dataset(subseq)
    device = torch.device('cpu')

    # create global map
    slam = PointFusion(device=device, odom="gt", dsratio=1)
    prev_frame = None
    pointclouds = Pointclouds(device=device)
    global_map = Pointclouds(device=device)
    for s in ds.ids:
        colors, depths, intrinsics, poses = ds[s]

        live_frame = RGBDImages(colors, depths, intrinsics, poses).to(device)
        pointclouds, _ = slam.step(pointclouds, live_frame, prev_frame)

        prev_frame = live_frame
        global_map.append_points(pointclouds)

    # visualize using open3d
    pc = pointclouds.points_list[0]
    pcd_gt = o3d.geometry.PointCloud()
    pcd_gt.points = o3d.utility.Vector3dVector(pc.cpu().detach().numpy())
    o3d.visualization.draw_geometries([pcd_gt])


def demo():
    import open3d as o3d

    subseq = "2011_09_26_drive_0002_sync"
    # subseq = "2011_09_26_drive_0005_sync"
    # subseq = "2011_09_26_drive_0023_sync"

    ds_depth = KITTIDepthSelection(subseq=subseq)
    ds_poses = KITTIRawPoses(subseq=subseq)

    poses = ds_poses.poses
    depth_poses = poses[ds_depth.ids]

    plt.figure()
    plt.title("%s" % subseq)
    plt.plot(poses[:, 0, 3], poses[:, 1, 3], '.')
    plt.plot(depth_poses[:, 0, 3], depth_poses[:, 1, 3], 'o')
    plt.grid()
    plt.xlabel('X [m]')
    plt.ylabel('Y [m]')
    plt.axis('equal')
    plt.show()

    global_map = list()
    # using poses convert pcs to one coord frame, create and visualize map
    for i in ds_depth.ids:
        rgb_img_raw, depth_img_raw, K = ds_depth[i]
        w, h = rgb_img_raw.shape[:2]

        rgb_img = o3d.geometry.Image(rgb_img_raw)
        depth_img = o3d.geometry.Image(np.asarray(depth_img_raw, dtype=np.uint16))
        rgbd_img = o3d.geometry.RGBDImage.create_from_color_and_depth(color=rgb_img, depth=depth_img)

        intrinsic = o3d.camera.PinholeCameraIntrinsic(width=w, height=h,
                                                      fx=K[0, 0], fy=K[1, 1], cx=K[0, 2], cy=K[1, 2])

        pcd = o3d.geometry.PointCloud.create_from_rgbd_image(image=rgbd_img, intrinsic=intrinsic)

        pose = ds_poses[i]
        pcd.transform(pose)

        # Flip it, otherwise the pointcloud will be upside down
        # pcd.transform([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]])
        # o3d.visualization.draw_geometries([pcd])

        global_map.append(pcd)

    o3d.visualization.draw_geometries(global_map)


def main():
    # poses_demo()
    # ts_demo()
    # gradslam_demo()
    demo()


if __name__ == '__main__':
    main()
