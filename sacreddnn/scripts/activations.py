## Add the option to modify activations on top of the basic script dnn.py
import os
import torch

import argparse
import torch.optim as optim
from torch import nn
from torch.utils.data import SubsetRandomSampler, DataLoader
import time
from tqdm import tqdm
import pandas as pd
import numpy as np
import tempfile

from sacred import Experiment
from sacred.commands import print_config
ex = Experiment('DNN')
# from sacred.utils import apply_backspaces_and_linefeeds # for progress bar captured output
# ex.captured_out_filter = apply_backspaces_and_linefeeds # doesn't work: sacred/issues/440
from sacred import SETTINGS 
SETTINGS['CAPTURE_MODE'] = 'no' # don't capture output (avoid progress bar clutter)


# add sacreddnn dir to path
import os, sys
sacreddnn_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, sacreddnn_dir) 

# SacredDNN imports 
from sacreddnn.parse_args import get_dataset, get_loss_function, get_model_type, get_optimizer
from sacreddnn.utils import num_params, l2_norm, run_and_config_to_path,\
                            file_observer_dir, to_gpuid_string
from sacreddnn.activations import replace_relu_
 

def square(input):
    return input * input # use torch.sigmoid to make sure that we created the most efficient implemetation based on builtin PyTorch functions

class SquareU(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, input):
        return square(input)

def train(loss, model, device, train_loader, optimizer):
    model.train()
    t = tqdm(train_loader) # progress bar integration
    train_loss, accuracy, ndata = 0, 0, 0    
    for data, target in t:
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        l = loss(output, target)
        l.backward()
        optimizer.step()

        train_loss += l.item()*len(data)
        pred = output.argmax(dim=1, keepdim=True)
        accuracy += pred.eq(target.view_as(pred)).sum().item()
        ndata += len(data)
        
        t.set_postfix(loss=train_loss/ndata, err=100*(1-accuracy/ndata))

def eval_loss_and_error(loss, model, device, loader):
    # t0 = time.time()
    model.eval()
    l, accuracy, ndata = 0, 0, 0
    with torch.no_grad():
        for data, target in loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            l += loss(output, target, reduction='sum').item()
            pred = output.argmax(dim=1, keepdim=True)
            accuracy += pred.eq(target.view_as(pred)).sum().item()
            ndata += len(data)
            
    # print(f"@eval time: {time.time()-t0:.4f}s")
    return l/ndata, (1-accuracy/ndata)*100

def get_xsize(args):
    xsize = [28,28]  if args.dataset in ["mnist","fashion"] else\
            [32,32]  if args.dataset in ["cifar10","cifar100"] else args.xsize
    return xsize

@ex.config  # Configuration is defined through local variables.
def cfg():
    batch_size = 128    # input batch size for training
    epochs = 100        # number of epochs to train
    lr = 0.1            # learning rate
    weight_decay = 5e-4 # weight decay param (=L2 reg. Good default is 5e-4)
    no_cuda = False     # disables CUDA training
    nthreads = 2        # number of threads
    save_model = False  # save model at checkpoints
    load_model = ""     # load model from path
    droplr = 5          # learning rate drop factor (use 0 or 1 for no-drop)
    opt = "nesterov"    # optimizer type
    loss = "nll"        # classification loss [nll, mse]
    model = "mlp_200_200"     # model type  [mlp_200_100, lenet, densenet121, resnet18, ...]
    dataset = "fashion" # dataset  [mnist, fashion, cifar10, cifar100]
    datapath = '~/data/'# folder containing the datasets (e.g. mnist will be in "data/MNIST")
    logtime = 2         # report every logtime epochs
    M = -1              # take only first M training  examples 
    Mtest = -1          # take only first Mtest test examples 
    preprocess = False  # data normalization
    gpu = 0             # gpu_id(s) to use

    ##Script specific options
    activation = None   # Change to e.g. "swish" to replace each relu of the model with a new activation
                        # ["swish", "quadu", "mish", ...]

    # To be done before any call to torch.cuda
    os.environ["CUDA_VISIBLE_DEVICES"] = to_gpuid_string(gpu)
    
@ex.automain
def main(_run, _config):
    args = argparse.Namespace(**_config)
    print_config(_run); print()
    logdir = file_observer_dir(_run)
    if not logdir is None:
        from torch.utils.tensorboard import SummaryWriter
        writer = SummaryWriter(log_dir=f"{logdir}/{run_and_config_to_path(_run, _config)}")

    if args.save_model: # make temp file. In the end, the model will be stored by the observers.
        save_path = tempfile.mkdtemp() + "/model.pt" 

    use_cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device(f"cuda" if use_cuda else "cpu")
    torch.set_num_threads(args.nthreads)
    print(f"USE_CUDA = {use_cuda},  DEVICE_COUNT={torch.cuda.device_count()}, NUM_CPU_THREADS={torch.get_num_threads()}")
    torch.manual_seed(args.seed)

    ## LOAD DATASET
    loader_args = {'pin_memory': True} if use_cuda else {}
    
    dtrain, dtest = get_dataset(args)
    train_idxs = list(range(len(dtrain) if args.M <= 0 else args.M))
    test_idxs = list(range(len(dtest) if args.Mtest <= 0 else args.Mtest))
    print(f"DATASET {args.dataset}: {len(train_idxs)} Train and {len(test_idxs)} Test examples")

    train_loader = DataLoader(dtrain,
        sampler=SubsetRandomSampler(train_idxs),
        batch_size=args.batch_size, **loader_args)
    test_loader = DataLoader(dtest,
        sampler=SubsetRandomSampler(test_idxs),
        batch_size=args.batch_size, **loader_args)

    ## BUILD MODEL
    Net = get_model_type(args)
    model = Net()
    replace_relu_(model, args.activation)
    model = model.to(device)
    if use_cuda and torch.cuda.device_count() > 1: 
        model = torch.nn.DataParallel(model)
    
    ex.info["num_params"] = num_params(model)
    ex.info["num_learnable"] = num_params(model, learnable=True)
    print(f"MODEL: {ex.info['num_params']} params, {ex.info['num_learnable']} learnable")

    ## CREATE OPTIMIZER
    optimizer = get_optimizer(args, model)
    if args.droplr:
        gamma_sched = 1/args.droplr if args.droplr > 0 else 1
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
            milestones=[args.epochs//2, args.epochs*3//4, args.epochs*15//16], gamma=gamma_sched) 

    ## LOSS FUNCTION
    loss = get_loss_function(args)

    ## REPORT CALLBACK
    def report(epoch):
        model.eval()
 
        o = dict() # store observations
        o["epoch"] = epoch
        o["lr"] = optimizer.param_groups[0]["lr"]
        o["train_loss"], o["train_error"] = \
            eval_loss_and_error(loss, model, device, train_loader)
        o["test_loss"], o["test_error"] = \
            eval_loss_and_error(loss, model, device, test_loader)
        o["norm"]  = l2_norm(model)

        print("\n", pd.DataFrame({k:[o[k]] for k in o}), "\n")
        for k in o:
            ex.log_scalar(k, o[k], epoch)
            if logdir:
                writer.add_scalar(k, o[k], epoch)
        
    ## START TRAINING
    report(0)
    for epoch in range(1, args.epochs + 1):
        train(loss, model, device, train_loader, optimizer)
        # torch.cuda.empty_cache()
        if epoch % args.logtime == 0:
            report(epoch)
        if epoch % 10 and args.save_model:
            torch.save(model.state_dict(), save_path)
            ex.add_artifact(save_path, content_type="application/octet-stream")    
        if args.droplr:
            scheduler.step()
            
    # Save model after training
    if args.save_model:
        ex.add_artifact(save_path, content_type="application/octet-stream")
