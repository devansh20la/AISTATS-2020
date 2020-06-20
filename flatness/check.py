import torch
import torch.nn as nn
from models import resnet18_narrow as resnet18
from utils import get_loader
from utils.train_utils import AverageMeter, accuracy
import argparse
from sklearn.model_selection import ParameterGrid
import pickle
from tqdm import tqdm 


def main(args):
    dset_loaders = get_loader(args, training=True)
    model = resnet18(args)

    if args.use_cuda:
        model.cuda()
        torch.backends.cudnn.benchmark = True

    model.load_state_dict(torch.load(f"{args.cp_dir}/trained_model.pth.tar"))
    model.eval()
    criterion = nn.CrossEntropyLoss()

    train_loss_mtr = AverageMeter()
    val_loss_mtr = AverageMeter()
    err1 = {'train': AverageMeter(), 'val': AverageMeter()}
    for phase in ["train", "val"]:
        for inp_data in dset_loaders[phase]:
            inputs, targets = inp_data

            if args.use_cuda:
                inputs, targets = inputs.cuda(), targets.cuda()

            with torch.set_grad_enabled(False):
                outputs = model(inputs)
                batch_loss = criterion(outputs, targets)
                batch_err = accuracy(outputs, targets, topk=(1, 5)) 
                if phase == "train":
                    train_loss_mtr.update(batch_loss.item(), inputs.size(0))
                    err1['train'].update(100.0 - batch_err[0].item(), inputs.size(0))
                elif phase == "val":
                    val_loss_mtr.update(batch_loss.item(), inputs.size(0))
                    err1['val'].update(100.0 - batch_err[0].item(), inputs.size(0))

    gen_gap = val_loss_mtr.avg - train_loss_mtr.avg
    return err1['train'].avg, err1['val'].avg, gen_gap


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--dir', type=str, default='.')
    parser.add_argument('--print_freq', type=int, default=50)
    parser.add_argument('--dtype', type=str, default="cifar10", help='Data type')
    parser.add_argument('--optim', type=str, default="sgd")
    parser.add_argument('--ep', type=int, default=250, help='Epochs')

    args = parser.parse_args()

    param_grid = {'m': [0.9, 0.5, 0.0],
                  'wd': [0.0, 1e-2, 1e-4],
                  'ms': [1],
                  'lr_decay': ["True", "False"],
                  'lr': [0.01, 0.001, 0.005],
                  'bs': [16, 32, 64, 128],
                  'skip': ["True", "False"],
                  'regular': ["batch_norm", "dropout", "none"]}

    check_grid = {'m': {'0.9': [0, 0], '0.5': [0, 0], '0.0': [0, 0]},
                  'wd': {'0.0': [0, 0], '0.01': [0, 0], '0.0001': [0, 0]},
                  'lr_decay': {"True": [0, 0], "False": [0, 0]},
                  'lr': {'0.01': [0, 0], '0.001': [0, 0], '0.005': [0, 0]},
                  'bs': {'16': [0, 0], '32': [0, 0], '64': [0, 0], '128': [0, 0]},
                  'skip': {"True": [0, 0], "False": [0, 0]},
                  'regular': {"batch_norm": [0, 0], "dropout": [0, 0], "none": [0, 0]}
                  }

    grid = list(ParameterGrid(param_grid))

    for exp_num, params in enumerate(tqdm(grid), 0):
        args.m = params['m']
        args.wd = params['wd']
        args.ms = params['ms']
        args.lr_decay = params['lr_decay']
        args.lr = params['lr']
        args.bs = params['bs']
        args.skip = params['skip']
        args.regular = params['regular']
        args.width = 1

        args.n = f"all_{args.dtype}/{exp_num}_{args.optim}_{args.ep}_{args.lr}" \
                 f"_{args.wd}_{args.bs}_{args.m}_{args.regular}_{args.skip}_{args.lr_decay}"

        check_grid['m'][f"{params['m']}"][0] += 1
        check_grid['wd'][f"{params['wd']}"][0] += 1
        check_grid['lr_decay'][f"{params['lr_decay']}"][0] += 1
        check_grid['lr'][f"{params['lr']}"][0] += 1
        check_grid['bs'][f"{params['bs']}"][0] += 1
        check_grid['skip'][f"{params['skip']}"][0] += 1
        check_grid['regular'][f"{params['regular']}"][0] += 1

        args.bs = 1024
        args.cp_dir = f"{args.dir}/checkpoints/{args.n}/run_ms_1"

        args.data_dir = f"{args.dir}/data/{args.dtype}"
        args.use_cuda = torch.cuda.is_available()

        train_err, val_err, gen_gap = main(args)

        if train_err < 1.0:
            check_grid['m'][f"{params['m']}"][1] += 1
            check_grid['wd'][f"{params['wd']}"][1] += 1
            check_grid['lr_decay'][f"{params['lr_decay']}"][1] += 1
            check_grid['lr'][f"{params['lr']}"][1] += 1
            check_grid['bs'][f"{params['bs']}"][1] += 1
            check_grid['skip'][f"{params['skip']}"][1] += 1
            check_grid['regular'][f"{params['regular']}"][1] += 1

        with open('results/all_results.csv', 'wa') as f:
            f.write(f"{args.n.split('/')[-1]}, {train_err}, {val_err}, {gen_gap}\n")

    with open('results/narrow_resnet_cifar10.csv', 'w') as f:
        f.write("hyper-parameter, good_exp, bad_exp\n")

    for k1, v1 in check_grid.items():
        for k2, v2 in v1.items():
            f.write(f"{k1}-{k2}, {v2[1]}, {v2[0]-v2[1]}\n")
