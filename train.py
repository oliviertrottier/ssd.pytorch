from data import *
from layers.modules import MultiBoxLoss
from ssd import build_ssd
import os
import sys
import time
import argparse
import re

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.utils.data as data
import numpy as np


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Neuron Object Detector using the Single Shot MultiBox Detector')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--config', type=str,
                    help='Name of configuration file')
parser.add_argument('--dataset_name', default='',
                    type=str, help='Name of the dataset')
parser.add_argument('--dataset_root', default='',
                    help='Dataset root directory path')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
                    help='Pretrained base model')
parser.add_argument('--batch_size', default=32, type=int,
                    help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('--resume_weights_only', default=False, type=str2bool,
                    help='Arguments to resume only weights (not epoch, lr, etc)')
parser.add_argument('--start_epoch', default=0, type=int,
                    help='Resume training at this epoch')
parser.add_argument('--num_workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, type=str2bool,
                    help='Use CUDA to train model')
parser.add_argument('--lr_init', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float,
                    help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
                    help='Weight decay for SGD')
parser.add_argument('--lr_decay', default=0.1, type=float,
                    help='Learning rate decay for SGD')
parser.add_argument('--visdom', default=False, type=str2bool,
                    help='Use visdom for loss visualization')
parser.add_argument('--weights_dir', default='weights/',
                    help='Directory for saving checkpoint models')
args = parser.parse_args()

# Build the configuration object.
configs = build_config(args.config)

# Add learning rate parameter in configs.
configs.train.lr = configs.train.lr_init

# Overwrite train arguments that have been passed.
input_args = get_passed_args(sys.argv)
input_args.pop('config')
for config_name in input_args.keys():
    if 'dataset' in config_name:
        main_config_name = config_name.replace('dataset_', '')
        setattr(configs.dataset, main_config_name, getattr(args, config_name))
    else:
        setattr(configs.train, config_name, getattr(args, config_name))

# Cuda configs
if configs.train.cuda:
    if not torch.cuda.is_available():
        raise Exception('Cuda is not available.')
    torch.set_default_tensor_type('torch.cuda.FloatTensor')
else:
    if torch.cuda.is_available():
        print("WARNING: A CUDA device is available, but not enabled.")
    torch.set_default_tensor_type('torch.FloatTensor')

# Create the weights directory.
if not os.path.exists(configs.output.weights_dir):
    os.mkdir(configs.output.weights_dir)

# Initialize visdom.
if configs.train.visdom:
    import visdom

    vis = visdom.Visdom()
    vis_legend = ['Loc Loss', 'Conf Loss', 'Total Loss']


def train():
    # Load dataset.
    dataset = TreeDataset(configs.dataset, transform=configs.dataset.augmentation)

    # Initialize net.
    net = build_ssd('train', configs.model)

    # Load weights.
    if configs.train.cuda:
        Map_loc = lambda storage, loc: storage
    else:
        Map_loc = 'cpu'

    if configs.train.resume:
        print('Resuming training. Loading {}...'.format(configs.train.resume))
        checkpoint = torch.load(configs.train.resume, map_location=Map_loc)
        if 'net_state' in checkpoint.keys():
            net.load_state_dict(checkpoint['net_state'])
            if not configs.train.resume_weights_only:
                print('Starting from epoch {}'.format(checkpoint['epoch']))
                configs.train.start_epoch = checkpoint['epoch']
                print('Adjusting the learning rate to: {}'.format(checkpoint['lr']))
                configs.train.lr = checkpoint['lr']
                adjust_learning_rate(configs.train.start_epoch)
                # optimizer.load_state_dict(checkpoint['optimizer_state'])
        else:
            print('Load weights only.')
            net.load_weights(configs.train.resume)
    else:
        print('Loading base network...')
        vgg_weights = torch.load(configs.model.basenet, map_location=Map_loc)
        net.vgg.load_state_dict(vgg_weights)

        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        net.extras.apply(weights_init)
        net.loc.apply(weights_init)
        net.conf.apply(weights_init)

    if configs.train.cuda:
        net = torch.nn.DataParallel(net)
        cudnn.benchmark = True
        net = net.cuda()

    # Initialize optimizer and criterion.
    optimizer = optim.SGD(net.parameters(), lr=configs.train.lr_init, momentum=configs.train.momentum,
                          weight_decay=configs.train.weight_decay)
    criterion = MultiBoxLoss(configs.model, 0.5, True, 0, True, 3, 0.5,
                             False, configs.train.cuda)
    net.train()
    print('Training SSD on:', dataset.name, 'for {} epochs.'.format(configs.train.num_epochs))
    print('Using the following configurations:')
    print(configs)

    # Initialize visdom plots.
    if configs.train.visdom:
        vis_title = 'Dendrites SSD on ' + dataset.name
        iter_plot = create_vis_plot(0, 0, 'Iteration', 'Loss', vis_title, vis_legend)
        epoch_plot = create_vis_plot(configs.train.start_epoch, 0, 'Epoch', 'Loss', vis_title, vis_legend)

    data_loader = data.DataLoader(dataset, configs.dataloader.batch_size,
                                  num_workers=configs.dataloader.num_workers,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=True)
    N_iterations = len(dataset)
    for epoch in range(configs.train.start_epoch, configs.train.num_epochs):
        # reset epoch losses
        epoch_loc_loss = 0
        epoch_conf_loss = 0
        epoch_total_loss = 0
        epoch_avg_loss = 0

        if epoch in configs.train.lr_schedule:
            adjust_learning_rate(epoch, optimizer)

        # loop through all batches
        t0 = time.time()
        for iteration, (images, targets) in enumerate(data_loader):
            if configs.train.cuda:
                images = Variable(images.cuda())
                targets = [Variable(ann.cuda(), volatile=True) for ann in targets]
            else:
                images = Variable(images)
                targets = [Variable(ann, volatile=True) for ann in targets]
            # forward prop
            out = net(images)

            # backward prop
            optimizer.zero_grad()
            loss_l, loss_c = criterion(out, targets)
            loss = loss_l + loss_c
            loss.backward()
            optimizer.step()

            # save epoch losses
            epoch_loc_loss += loss_l.data[0]
            epoch_conf_loss += loss_c.data[0]
            epoch_total_loss = epoch_loc_loss + epoch_conf_loss
            epoch_avg_loss = epoch_total_loss / ((iteration + 1) * configs.dataloader.batch_size)

            # monitoring
            if iteration % 10 == 0:
                t1 = time.time()
                print("Iteration {:4d} || Epoch Avg Loss {:.4f} || timer: {:.2f} s".format(iteration, epoch_avg_loss,
                                                                                           (t1 - t0)))
                t0 = time.time()

            # update iteration loss plot.
            if configs.train.visdom:
                update_vis_plot(iteration + 1, iter_plot, loss_l.data[0], loss_c.data[0])

        # update epoch loss plot.
        if configs.train.visdom:
            update_vis_plot(epoch + 1, epoch_plot, epoch_loc_loss / N_iterations, epoch_conf_loss / N_iterations)

        # save checkpoint.
        if epoch != 0 and epoch % 2 == 0:
            print('Saving checkpoint, epoch:', epoch)
            if configs.train.cuda:
                net_weights = net.module
            else:
                net_weights = net
            checkpoint_filename = 'ssd300_' + configs.dataset.name + '_' + repr(epoch) + '.pth'
            checkpoint_path = os.path.join(configs.output.weights_dir, checkpoint_filename)
            save_checkpoint(net_weights, configs.train.lr, epoch, epoch_loc_loss, epoch_conf_loss,
                            epoch_total_loss, epoch_avg_loss, checkpoint_path)

    # save final state.
    if configs.train.cuda:
        net_weights = net.module
    else:
        net_weights = net
    checkpoint_filename = 'ssd300_' + configs.dataset.name + '_Final.pth'
    checkpoint_path = os.path.join(configs.output.weights_dir, checkpoint_filename)
    save_checkpoint(net_weights, configs.train.lr, epoch, epoch_loc_loss, epoch_conf_loss,
                    epoch_total_loss, epoch_avg_loss, checkpoint_path)


def adjust_learning_rate(epoch, optimizer=None):
    """Sets the learning rate to the initial LR decayed by lr_decay at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    configs.train.lr = configs.train.lr_init * \
                       (configs.train.lr_decay ** sum(np.array(configs.train.lr_schedule) <= epoch))
    if optimizer:
        for param_group in optimizer.param_groups:
            param_group['lr_init'] = configs.train.lr


def xavier(param):
    init.xavier_uniform(param)


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        m.bias.data.zero_()


def save_checkpoint(net, lr, epoch, epoch_loc_loss, epoch_conf_loss, epoch_total_loss, epoch_avg_loss, filename):
    checkpoint_dict = {'epoch': epoch + 1,
                       'net_state': net.state_dict(),
                       'lr': lr,
                       # 'optimizer_state': optimizer.state_dict(),
                       'loc_loss': epoch_loc_loss,
                       'conf_loss': epoch_conf_loss,
                       'total_loss': epoch_total_loss,
                       'avg_loss': epoch_avg_loss}
    torch.save(checkpoint_dict, filename)


def create_vis_plot(x_init, y_init, _xlabel, _ylabel, _title, _legend):
    return vis.line(
        X=x_init * torch.ones((1,)).cpu(),
        Y=y_init * torch.ones((1, 3)).cpu(),
        opts=dict(
            xlabel=_xlabel,
            ylabel=_ylabel,
            title=_title,
            legend=_legend
        )
    )


def update_vis_plot(iteration, window_id, loc, conf):
    vis.line(
        X=torch.ones((1, 3)).cpu() * iteration,
        Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu(),
        win=window_id,
        update='append', opts=dict(legend=vis_legend)
    )


if __name__ == '__main__':
    train()
