import gradslam
import open3d as o3d
import matplotlib.pyplot as plt
import torch
import os
from PIL import Image
from scipy import interpolate
import numpy as np
from .models import SparseConvNet
from .metrics import MAE, RMSE, localization_accuracy


def load_model(path=None):
    model = SparseConvNet()
    if path:
        if os.path.exists(path):
            print('Loading model weights from %s' % path)
            model.load_state_dict(torch.load(path, map_location='cpu'))
        else:
            print('No model weights found!!!')
    return model


def plot_depth(depth_sparse, depth_pred, depth_gt, episode, mode, visualize=False, log_dir=None):
    # convert depth into np images
    depth_img_gt_np = depth_gt.detach().cpu().numpy().squeeze()
    depth_img_sparse_np = depth_sparse.detach().cpu().numpy().squeeze()
    pred_np = depth_pred.detach().cpu().numpy().squeeze()

    # plot images
    fig = plt.figure()
    ax = fig.add_subplot(3, 1, 1)
    plt.imshow(depth_img_sparse_np)
    ax.set_title('Sparse')
    ax = fig.add_subplot(3, 1, 2)
    plt.imshow(pred_np)
    ax.set_title('Prediction')
    ax = fig.add_subplot(3, 1, 3)
    plt.imshow(depth_img_gt_np)
    ax.set_title('Ground truth')
    fig.tight_layout(h_pad=1)
    if log_dir is not None:
        plt.savefig(os.path.join(log_dir, f'plot-{mode}-{episode}.png'))
    if visualize:
        plt.show()
    plt.close(fig)


def plot_pc(pc, episode, mode, visualize=False, log_dir=None):
    """
    Args:
        pc: <gradslam.Pointclouds> or <torch.Tensor>
    """
    if isinstance(pc, gradslam.Pointclouds):
        pcd = pc.open3d(0)
    elif isinstance(pc, torch.Tensor):
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(pc.detach().cpu().view(-1, 3))
    else:
        raise ValueError('Input should be gradslam.Pointclouds or torch.Tensor')
    # Flip it, otherwise the point cloud will be upside down
    pcd.transform([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]])
    if log_dir is not None:
        o3d.io.write_point_cloud(os.path.join(log_dir, f'map-{mode}-{episode}.pcd'), pcd)
    if visualize:
        o3d.visualization.draw_geometries([pcd])


def plot_metric(metric, metric_title, visualize=False, log_dir=None, val_scaling=None):
    """
    Plots graph of metric over episodes
    Args:
        metric: list of <torch.tensor>
        metric_title: string
    """
    fig = plt.figure()
    if val_scaling is None:
        x_ax = [i for i in range(len(metric))]
    else:
        x_ax = [i*val_scaling for i in range(len(metric))]
    y_ax = [loss.detach().cpu().numpy() for loss in metric]
    plt.plot(x_ax, y_ax)
    plt.xlabel('Episode')
    plt.ylabel(metric_title)
    plt.title(f'{metric_title} over episodes')
    if log_dir is not None:
        plt.savefig(os.path.join(log_dir, f'{metric_title}.png'))
    if visualize:
        plt.show()
    plt.close(fig)


def compute_val_metrics(depth_gt, depth_pred, traj_gt, traj_pred):
    rmse = RMSE(depth_gt, depth_pred)
    mae = MAE(depth_gt, depth_pred)
    loc_acc = localization_accuracy(traj_gt, traj_pred)
    return rmse, mae, loc_acc


def metrics_dataset(dataset_gt, dataset):
    """
    Computes mean MAE and RMSE values between two datasets
    """
    assert len(dataset) == len(dataset_gt)
    mae_data = 0
    rmse_data = 0
    for i in range(len(dataset)):
        colors, depths, intrinsics, poses = dataset[i]
        colors_gt, depths_gt, intrinsics_gt, poses_gt = dataset_gt[i]
        mae = MAE(depths_gt, depths)
        rmse = RMSE(depths_gt, depths)
        mae_data += mae
        rmse_data += rmse

    return mae_data / len(dataset), rmse_data / len(dataset)


def save_gradslam_image(img, img_path):
    """
    Save depth image from
    :param img_torch: <torch.tensor> of shape (B, S, H, W, CH)
    """
    img = torch.squeeze(img, 0)
    img = torch.squeeze(img, 0)
    img = img.cpu().detach().numpy()
    img *= (2 ** 8)     # shift values range
    data_pil = Image.fromarray(np.squeeze(img.astype(np.uint16)), mode='I;16').convert(mode='I')
    data_pil.save(img_path)


def convert_img_label(name):
    default = "0000000000"
    name = str(name)
    return default[:-len(name)] + name


def complete_sequence(model, dataset, path_to_save, subseq, camera='left', replace=False):
    """
    Runs depth images through the model and saves them as a KITTI compatible sequence
    :param path_to_save: path to KITTI depth files (e.g. KITTI/depth/train)
    """
    image_folder = "image_02" if camera == 'left' else "image_03"

    subfolders = [subseq, "proj_depth", "prediction", image_folder]
    for subfold in subfolders:
        path_to_save = os.path.join(path_to_save, subfold)
        if not os.path.isdir(path_to_save):
            os.mkdir(path_to_save)

    for i in range(len(dataset)):
        img_name = convert_img_label(dataset.ids[i]) + ".png"
        img_path = os.path.join(path_to_save, img_name)
        if os.path.exists(img_path) and not replace:
            continue
        elif os.path.exists(img_path) and replace:
            os.remove(img_path)
        colors, depths, intrinsics, poses = dataset[i]
        mask = (depths > 0).float()
        with torch.no_grad():
            pred = model(depths, mask)
        save_gradslam_image(pred, img_path)


def save_preds_demo():
    from depth_completion.data import Dataset
    from tqdm import tqdm

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model = load_model(os.path.realpath(os.path.join(os.path.dirname(__file__), '../../config/weights/weights.pth')))
    model = model.to(device)
    model.eval()

    depth_set = 'val'
    assert depth_set == 'train' or depth_set == 'val'
    camera = 'right'
    assert camera == 'left' or camera == 'right'

    path_to_save = os.path.join(os.path.dirname(__file__), '../../data/KITTI/depth/%s' % depth_set)

    for subseq in tqdm(sorted(os.listdir(path_to_save))):
        print('Processing sequence: %s' % subseq)

        ds = Dataset(subseq, depth_type="sparse", depth_set=depth_set, camera=camera, zero_origin=False, device=device)

        complete_sequence(model=model,
                          dataset=ds,
                          path_to_save=os.path.realpath(path_to_save),
                          subseq=subseq,
                          camera=camera)


if __name__ == '__main__':
    save_preds_demo()
