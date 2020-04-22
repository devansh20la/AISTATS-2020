from args import get_args
import torch
import torch.nn as nn
import torch.optim as optim
from models import resnet18_narrow as resnet18
import time
import logging
import numpy as np
import glob
import os
import random
from torch.utils.tensorboard import SummaryWriter
from utils import get_loader, class_model_run


def create_path(path):
    if os.path.isdir(path) is False:
        os.makedirs(path)


def main(args):
    logger = logging.getLogger('my_log')

    dset_loaders = get_loader(args, training=True)
    model = resnet18(args)
    criterion = nn.CrossEntropyLoss()

    if args.use_cuda:
        model.cuda()
        torch.backends.cudnn.benchmark = True

    if args.optim == "sgd":
        optimizer = optim.SGD(model.parameters(), lr=args.lr,
                              momentum=args.m, weight_decay=args.wd)
    elif args.optim == "adam":
        optimizer = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999), eps=1e-08,
                               weight_decay=args.wd, amsgrad=False)
    if args.lr_decay:
        scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.1)

    writer = SummaryWriter(log_dir=args.cp_dir)

    for epoch in range(args.ep):

        logger.info('Epoch: [%d | %d]' % (epoch, args.ep))

        trainloss, trainerr1, trainerr5 = \
            class_model_run('train', dset_loaders,
                            model, criterion, optimizer, args)

        logger.info('Train_Loss = {0}, Train_Err = {1}'.format(trainloss, trainerr1))
        writer.add_scalar('Train/Train_Loss', trainloss, epoch)
        writer.add_scalar('Train/Train_Err1', trainerr1, epoch)
        writer.add_scalar('Train/Train_Err5', trainerr5, epoch)

        if args.lr_decay:
            scheduler.step()

        if epoch % 50 == 0:
            valloss, valerr1, valerr5 = \
                class_model_run('val', dset_loaders, model,
                                criterion, optimizer, args)
            logger.info('Val_Loss = {0}, Val_Err = {1}'.format(valloss, valerr1))
            writer.add_scalar('Val/Val_Loss', valloss, epoch)
            writer.add_scalar('Val/Val_Err1', valerr1, epoch)
            writer.add_scalar('Val/Val_Err5', valerr5, epoch)

        state = model.state_dict()
        torch.save(state, f"{args.cp_dir}/trained_model.pth.tar")


if __name__ == '__main__':
    args = get_args()

    # Random seed
    random.seed(args.ms)
    torch.manual_seed(args.ms)
    np.random.seed(args.ms)
    if args.use_cuda:
        torch.cuda.manual_seed_all(args.ms)

    # Intialize directory and create path
    args.cp_dir = f"{args.dir}/checkpoints/{args.n}"
    list_of_files = sorted(glob.glob1(args.cp_dir, '*run*'))
    if len(list_of_files) == 0:
        list_of_files = 0
    else:
        list_of_files = len(list_of_files)

    args.cp_dir = f"{args.cp_dir}/run{list_of_files}"
    create_path(args.cp_dir)

    # Logging tools
    logger = logging.getLogger('my_log')
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(
        args.cp_dir, time.strftime("%Y%m%d-%H%M%S") + '.log'))
    logger.addHandler(fh)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(console)
    logger.info(args)

    #holy grail that matches params to run
    path = f"{args.dir}/checkpoints/{args.n}/holygrail.txt"
    if not(os.path.isfile(path)):
        with open(path, 'w') as f:
            for key in vars(args):
                f.write(f"{key},")
            f.write("\n")

    with open(path, 'a') as f:
        for key, value in vars(args).items():
            f.write(f"{value},")
        f.write("\n")

    main(args)