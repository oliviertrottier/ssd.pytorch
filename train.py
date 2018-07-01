from data import *
from utils.augmentations import SSDAugmentation
from layers.modules import MultiBoxLoss
from ssd import build_ssd
import os
import sys
import time
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.utils.data as data
import numpy as np
import argparse


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Single Shot MultiBox Detector Training With Pytorch')
train_set = parser.add_mutually_exclusive_group()
parser.add_argument('--dataset', default='VOC',
                    type=str, help='VOC or COCO')
parser.add_argument('--dataset_root', default=VOC_ROOT,
                    help='Dataset root directory path')
parser.add_argument('--basenet', default='vgg16_reducedfc.pth',
                    help='Pretrained base model')
parser.add_argument('--batch_size', default=32, type=int,
                    help='Batch size for training')
parser.add_argument('--resume', default=None, type=str,
                    help='Checkpoint state_dict file to resume training from')
parser.add_argument('--start_iter', default=0, type=int,
                    help='Resume training at this iter')
parser.add_argument('--num_workers', default=4, type=int,
                    help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True, type=str2bool,
                    help='Use CUDA to train model')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                    help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float,
                    help='Momentum value for optim')
parser.add_argument('--weight_decay', default=5e-4, type=float,
                    help='Weight decay for SGD')
parser.add_argument('--gamma', default=0.1, type=float,
                    help='Gamma update for SGD')
parser.add_argument('--visdom', default=False, type=str2bool,
                    help='Use visdom for loss visualization')
parser.add_argument('--save_folder', default='weights/',
                    help='Directory for saving checkpoint models')
args = parser.parse_args()


if torch.cuda.is_available():
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not args.cuda:
        print("WARNING: It looks like you have a CUDA device, but aren't " +
              "using CUDA.\nRun with --cuda for optimal training speed.")
        torch.set_default_tensor_type('torch.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')

# Make the default save_folder a subdirectory of the script folder.
script_path = os.path.dirname(os.path.realpath(sys.argv[0])) + '/'
if args.save_folder == parser.get_default('save_folder'):
    args.save_folder = os.path.join(script_path, args.save_folder)

if not os.path.exists(args.save_folder):
    os.mkdir(args.save_folder)


def train():
    if args.dataset == 'COCO':
        if args.dataset_root == VOC_ROOT:
            if not os.path.exists(COCO_ROOT):
                parser.error('Must specify dataset_root if specifying dataset')
            print("WARNING: Using default COCO dataset_root because " +
                  "--dataset_root was not specified.")
            args.dataset_root = COCO_ROOT
        dataset_config = coco
        dataset = COCODetection(root=args.dataset_root,
                                transform=SSDAugmentation(dataset_config['min_dim'],
                                                          MEANS))
    elif args.dataset == 'VOC':
        if args.dataset_root == COCO_ROOT:
            parser.error('Must specify dataset if specifying dataset_root')
        dataset_config = voc
        dataset = VOCDetection(root=args.dataset_root,
                               transform=SSDAugmentation(dataset_config['min_dim'],
                                                         MEANS))
    elif args.dataset in ['Tree28_synthesis1', 'Tree29_synthesis1']:
        dataset_config = tree_synth0_config
        dataset = TreeDataset(root=args.dataset_root, name=args.dataset,
                           transform=SSDAugmentation(dataset_config['min_dim'],
                                                     dataset_config['pixel_means']))
    elif args.dataset in ['Tree28_synthesis2', 'Tree29_synthesis2']:
        dataset_config = tree_synth1_config
        dataset = TreeDataset(root=args.dataset_root, name=args.dataset,
                           transform=SSDAugmentation(dataset_config['min_dim'],
                                                     dataset_config['pixel_means']))
    elif args.dataset in ['Tree28_synthesis3', 'Tree29_synthesis3', 'Tree30_synthesis4']:
        dataset_config = tree_synth2_config
        dataset = TreeDataset(root=args.dataset_root, name=args.dataset,
                          transform=SSDAugmentation(dataset_config['min_dim'],
                                                    dataset_config['pixel_means']))
    else:
        raise ValueError('The dataset is not defined.')

    if args.visdom:
        import visdom
        viz = visdom.Visdom()

    ssd_net = build_ssd('train', dataset_config)
    net = ssd_net

    if args.cuda:
        net = torch.nn.DataParallel(ssd_net)
        cudnn.benchmark = True

    if args.resume:
        print('Resuming training, loading {}...'.format(args.resume))
        ssd_net.load_weights(args.resume)
    else:
        vgg_weights = torch.load(args.save_folder + args.basenet)
        print('Loading base network...')
        ssd_net.vgg.load_state_dict(vgg_weights)

    if args.cuda:
        net = net.cuda()

    if not args.resume:
        print('Initializing weights...')
        # initialize newly added layers' weights with xavier method
        ssd_net.extras.apply(weights_init)
        ssd_net.loc.apply(weights_init)
        ssd_net.conf.apply(weights_init)

    optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=args.momentum,
                          weight_decay=args.weight_decay)
    criterion = MultiBoxLoss(dataset_config, 0.5, True, 0, True, 3, 0.5,
                             False, args.cuda)

    net.train()
    print('Training SSD on:', dataset.name, 'for {} epochs.'.format(dataset_config['N_epochs']))
    print('Using the specified args:')
    print(args)

    N_iterations = len(dataset) // args.batch_size
    step_index = 0

    if args.visdom:
        vis_title = 'SSD.PyTorch on ' + dataset.name
        vis_legend = ['Loc Loss', 'Conf Loss', 'Total Loss']
        iter_plot = create_vis_plot('Iteration', 'Loss', vis_title, vis_legend)
        epoch_plot = create_vis_plot('Epoch', 'Loss', vis_title, vis_legend)

    data_loader = data.DataLoader(dataset, args.batch_size,
                                  num_workers=args.num_workers,
                                  shuffle=True, collate_fn=detection_collate,
                                  pin_memory=True)

    for epoch in range(dataset_config['N_epochs']):
        if args.visdom and epoch != 0:
            update_vis_plot(epoch, loc_loss, conf_loss, epoch_plot, None,
                            'append', N_iterations)

        # Reset epoch loss counters
        loc_loss = 0
        conf_loss = 0

        if epoch in dataset_config['lr_steps']:
            step_index += 1
            adjust_learning_rate(optimizer, args.gamma, step_index)

        # Loop through all batches.
        #batch_iterator = iter(data_loader)
        #for iter in range(N_iterations):
        # images, targets = next(batch_iterator)
        t0 = 0
        for iteration, loaded_data in enumerate(data_loader):
            # Get images and targets.
            images, targets = loaded_data
            if args.cuda:
                images = Variable(images.cuda())
                targets = [Variable(ann.cuda(), volatile=True) for ann in targets]
            else:
                images = Variable(images)
                targets = [Variable(ann, volatile=True) for ann in targets]
            # Forward prop
            out = net(images)

            # Backward prop
            optimizer.zero_grad()
            loss_l, loss_c = criterion(out, targets)
            loss = loss_l + loss_c
            loss.backward()
            optimizer.step()

            # Store loss
            loc_loss += loss_l.data[0]
            conf_loss += loss_c.data[0]

            # Monitoring
            if iteration % 10 == 0:
                t1 = time.time()
                print("Iteration {0:4d} || Loss {.4f} || timer: {.3f} s".format(iteration, loss.data[0], (t1 - t0)))
                t0 = time.time()

            if args.visdom:
                update_vis_plot(iteration, loss_l.data[0], loss_c.data[0],
                                iter_plot, epoch_plot, 'append')

        # Save checkpoint.
        if epoch != 0 and epoch % 2 == 0:
            print('Saving state, epoch:', epoch)
            torch.save(ssd_net.state_dict(), args.save_folder + 'ssd300_' + args.dataset + '_' + repr(epoch) + '.pth')
    torch.save(ssd_net.state_dict(), args.save_folder + args.dataset + '.pth')


def adjust_learning_rate(optimizer, gamma, step):
    """Sets the learning rate to the initial LR decayed by 10 at every
        specified step
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    lr = args.lr * (gamma ** (step))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def xavier(param):
    init.xavier_uniform(param)


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        xavier(m.weight.data)
        m.bias.data.zero_()


def create_vis_plot(_xlabel, _ylabel, _title, _legend):
    return viz.line(
        X=torch.zeros((1,)).cpu(),
        Y=torch.zeros((1, 3)).cpu(),
        opts=dict(
            xlabel=_xlabel,
            ylabel=_ylabel,
            title=_title,
            legend=_legend
        )
    )


def update_vis_plot(iteration, loc, conf, window1, window2, update_type,
                    epoch_size=1):
    viz.line(
        X=torch.ones((1, 3)).cpu() * iteration,
        Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu() / epoch_size,
        win=window1,
        update=update_type
    )
    # initialize epoch plot on first iteration
    if iteration == 0:
        viz.line(
            X=torch.zeros((1, 3)).cpu(),
            Y=torch.Tensor([loc, conf, loc + conf]).unsqueeze(0).cpu(),
            win=window2,
            update=True
        )


if __name__ == '__main__':
    train()
