## Train a replicated system (robust ensemble)
import torch
import argparse
import torch.optim as optim
from torch.utils.data import SubsetRandomSampler, DataLoader
import time
import os
from tqdm import tqdm
import pandas as pd
import numpy as np
import tempfile
from shutil import copyfile
from copy import deepcopy

from sacred import Experiment
from sacred.commands import print_config
ex = Experiment('RobustDNN')
from sacred import SETTINGS 
SETTINGS['CAPTURE_MODE'] = 'no'

# add sacreddnn dir to path
import os, sys
sacreddnn_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, sacreddnn_dir) 

# SacredDNN imports
from sacreddnn.models.robust import RobustNet, RobustDataLoader
from sacreddnn.parse_args import get_dataset, get_loss_function, get_model_type, get_optimizer
from sacreddnn.utils import num_params, l2_norm, run_and_config_to_path,\
                            file_observer_dir, to_gpuid_string, take_n_per_class
from sacreddnn.activations import replace_relu_
from sacreddnn.warmup_scheduler import GradualWarmupScheduler

def topk_acc(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        #batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            #res.append(correct_k.mul_(100.0 / batch_size))
            res.append(correct_k)
        return res

def train_esgd(loss, model, train_loader, optimizer, scheduler, y=1, g_time=1, file=None):
    model.train()
    t = tqdm(train_loader) # progress bar integration
    train_loss, accuracy, ndata, meandist, mean_grad, b_count = 0, 0, 0, 0, 0, 0
    for counter, (data1, target1) in enumerate(t):
        def helper():
            def feval():
                data, target = next(iter(train_loader.single_loader()))
                target = target.to(model.master_device)
                model.zero_grad()
                output = model(data)
                l = loss(output, target)
                l.backward()
                pred = output.argmax(dim=1, keepdim=True)
                acc = pred.eq(target.view_as(pred)).sum().item()
                return (l.item(), acc)
            return feval
        
        myl, acc, dist, norm_grad, lr_out, g_esgd = optimizer.step(helper(), model, loss, scheduler)
        file.write('{}\n'.format(dist))

        train_loss += myl*len(data1)
        accuracy += acc
        ndata += len(data1)
        meandist += dist
        mean_grad += norm_grad
        b_count += 1

        t.set_postfix(loss=train_loss/ndata, err=100*(1-accuracy/ndata), grad=norm_grad, dist=dist)
        
    return meandist/b_count, mean_grad/b_count, lr_out, g_esgd
    
def eval_loss_and_error(loss, model, loader, train=False):
    # t0 = time.time()
    model.eval()
    center = model.get_or_build_center()
    center.eval()

    l, accuracy = np.zeros(model.y), np.zeros(model.y)
    center_loss, center_accuracy, center_accuracy5 = 0., 0., 0.
    ensemble_loss, ensemble_accuracy = 0., 0.
    ndata = 0
    # committee_loss, committee_accuracy = 0., 0. # TODO
    with torch.no_grad():
        for data, target in loader.single_loader():
            # single replicas
            outputs = model(data, split_input=False, concatenate_output=False)
            target = target.to(model.master_device)
            for a, output in enumerate(outputs):
                l[a] += loss(output, target, reduction='sum').item()
                pred = output.argmax(dim=1, keepdim=True)
                accuracy[a] += pred.eq(target.view_as(pred)).sum().item()
            
            # center
            output = center(data.to(model.master_device))
            center_loss += loss(output, target, reduction='sum').item()
            pred = output.argmax(dim=1, keepdim=True)
            center_accuracy += pred.eq(target.view_as(pred)).sum().item()
            if not train:
                acc5 = topk_acc(output, target, topk=(5,))
                center_accuracy5 += acc5[0].item()
            ndata += len(data) 


    # print(f"@eval time: {time.time()-t0:.4f}s")
    l /= ndata
    accuracy /= ndata
    center_loss /= ndata
    center_accuracy /= ndata
    center_accuracy5 /= ndata

    num_params_center = sum(p.numel() for p in center.parameters())
    norm_center = 0.0
    for wc in center.parameters():
        norm_center += wc.norm()**2
    norm_center = np.sqrt(norm_center.item() / num_params_center)
    
    return l, (1-accuracy)*100, center_loss, (1-center_accuracy)*100, (1-center_accuracy5)*100, norm_center

def create_perturb(model):
    z = []
    for p in model.parameters():
        r = p.clone().detach().normal_()
        z.append(p.data * r)
    return z
    
def perturb(model, z, noise_ampl):
    for i,p in enumerate(model.parameters()):
        p.data += noise_ampl * z[i]

def initialization_rescaling(model, gain_factor):
    for i,p in enumerate(model.parameters()):
        p.data *= gain_factor


@ex.config  # Configuration is defined through local variables.
def cfg():
    batch_size = 128       # input batch size for training
    epochs = 100           # number of epochs to train
    lr = 0.1               # learning rate
    weight_decay = 5e-4    # weight decay param (=L2 reg. Good value is 5e-4)
    mom = 0.9              # momentum
    dropout = 0.           # dropout
    no_cuda = False        # disables CUDA training
    nthreads = 2           # number of threads
    save_model = False     # save current model to path
    save_epoch = -1        # save every save_epoch model
    load_model = ""        # load model from path
    last_epoch = 0         # last_epoch for schedulers
    droplr = 5             # learning rate drop factor (use 0 for no-drop)
    drop_mstones = "drop_150_225" # learning rate milestones (epochs at which applying the drop factor)
    warmup = False         # GradualWarmupScheduler
    opt = "entropy-sgd"    # optimizer type
    loss = "nll"           # classification loss [nll, mse]
    flood = 0.             # flood value (train loss will not go under this value)
    model = "lenet"        # model type  [lenet, densenet, resnet_cifar, efficientnet-b{1-7}(-pretrained)]  
    dataset = "fashion"    # dataset  [mnist, fashion, cifar10, cifar100]
    datapath = '~/data/'   # folder containing the datasets (e.g. mnist will be in "data/MNIST")
    logtime = 2            # report every logtime epochs
    #M = -1                # take only first M training examples 
    #Mtest = -1            # take only first Mtest test examples 
    #pclass = -1           # take only pclass training examples for each class 
    preprocess = 0         # data preprocessing level. preprocess=0 is no preproc. Check preproc_transforms.py
    gpu = 0                # which gpu(s) to use, if 'distribute' all gpus are used
    deterministic = False  # set deterministic mode on gpu
    #noise_ampl = 0.       # noise amplitude (for perturbing model initialization)
    #init_rescale = 0.     # rescaling of model initialization

    # non-trivial data augmentations ((Fast)AutoAugment, CutOut)
    augm_type = 'autoaug_cifar10'
    cutout = 0
    
    ## ROBUST ENSEMBLE SPECIFIC
    y = 1                      # number of replicas
    g = 0.                     # initial coupling value. If None, balance training and coupling loss at epoch 0
    grate = 0.                 # coupling increase rate
    gmax = 1e4                 # total coupling multiplicative factor
    gsched = "exp"             # exp, lin, cosine
    gtime = 1

    # Entropy-SGD specific
    L = 1
    sgld_noise = 1e-4
    sgld_lr = 0.1
    alpha = 0.75
    gscale = True
    mom_sgld = 0.9              # sgld momentum
    
    ## CHANGE ACTIVATIONS
    activation = None   # Change to e.g. "swish" to replace each relu of the model with a new activation
                        # ["swish", "quadu", "mish", ...]

@ex.automain
def main(_run, _config):
    ## SOME BOOKKEEPING
    args = argparse.Namespace(**_config)
    print_config(_run); print()
    logdir = file_observer_dir(_run)
    if not logdir is None:
        from torch.utils.tensorboard import SummaryWriter
        writer = SummaryWriter(log_dir=f"{logdir}/{run_and_config_to_path(_run, _config)}")

    if args.save_model: # make temp file. In the end, the model will be stored by the observers.
        save_prefix = tempfile.mkdtemp() + "/model"

    use_cuda = not args.no_cuda and torch.cuda.is_available()
    if use_cuda:
        if args.gpu=="distribute":
            ndevices = torch.cuda.device_count()
            devices = [torch.device(y%ndevices) for y in range(args.y+1)]
        elif isinstance(args.gpu, int):
            devices = [torch.device(args.gpu)]*(args.y+1)
        else:
            if not isinstance(args.gpu, list):
                raise ValueError("please provide gpu as list")
            l=len(args.gpu)
            devices = [torch.device(args.gpu[r%l]) for r in range(args.y+1)]
    else:
        devices = [torch.device("cpu")]*(args.y+1)
    for r, device in enumerate(devices):
        print("{} on {}".format("replica {}".format(r) if r<len(devices)-1 else "master", device))

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    if args.deterministic:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    
    torch.set_num_threads(args.nthreads)

    ## LOAD DATASET
    #loader_args = {'pin_memory': True} if use_cuda else {}
    loader_args = {}
    dtrain, dtest = get_dataset(args)
    #if args.pclass > 0:
    #    train_idxs = take_n_per_class(dtrain, args.pclass)
    #else:
    #train_idxs = list(range(len(dtrain) if args.M <= 0 else args.M))
    train_idxs = list(range(len(dtrain)))

    #test_idxs = list(range(len(dtest) if args.Mtest <= 0 else args.Mtest))
    test_idxs = list(range(len(dtest)))
    
    print(f"DATASET {args.dataset}: {len(train_idxs)} Train and {len(test_idxs)} Test examples")

    train_loader = RobustDataLoader(dtrain,
        y=args.y, concatenate=True, num_workers=0,
        sampler=SubsetRandomSampler(train_idxs),
        batch_size=args.batch_size, **loader_args)
    test_loader = RobustDataLoader(dtest,
        y=args.y, concatenate=True, num_workers=0,
        sampler=SubsetRandomSampler(test_idxs),
        batch_size=args.batch_size, **loader_args)
    
    ## BUILD MODEL
    Net = get_model_type(args)
    model = RobustNet(Net, y=args.y, g=args.g, grate=args.grate, gmax=args.gmax, devices=devices, use_center=False, Tmax=args.epochs)
    
    model2 = RobustNet(Net, y=args.y, g=args.g, grate=args.grate, gmax=args.gmax, devices=devices, use_center=False, Tmax=args.epochs)

    #replace activation function
    if args.activation != 'relu':
        replace_relu_(model, args.activation)
        
    # rescale initialization
    #if args.init_rescale > 0.:
    #    initialization_rescaling(model, args.init_rescale)

    # perturb initialization
    #if args.noise_ampl > 0:
    #    z = create_perturb(model)
    #    perturb(model, z, args.noise_ampl)
    #    #ampl = args.noise_ampl * l2_norm(model, mediate=True)
    #    #perturb(model, z, ampl)

    if args.load_model:
        model.load_state_dict(torch.load(args.load_model + ".pt"))
    ex.info["num_params"] = num_params(model)
    print(f"MODEL: {ex.info['num_params']} params")

    ## CREATE OPTIMIZER
    optimizer = get_optimizer(args, model)
        
    if args.droplr:
        
        if args.droplr == 'cosine':
            scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=0., last_epoch=-1)
            print("Cosine lr schedule")
            
        elif args.droplr < 1. and args.droplr != 0.:
            scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=2, gamma=args.droplr)
        else:
            gamma_sched = 1. / args.droplr if args.droplr > 0 else 1
            if args.drop_mstones is None:
                mstones = [args.epochs//2, args.epochs*3//4, args.epochs*15//16]
            else:
                mstones = [int(h) for h in args.drop_mstones.split('_')[1:]]
            print("LR MileStones %s" % mstones)
            print("Custom lr schedule")
                
            scheduler = optim.lr_scheduler.MultiStepLR(optimizer,\
                        milestones=mstones, gamma=gamma_sched)
    else:
        scheduler = None

    if args.warmup:
        scheduler = GradualWarmupScheduler(
            optimizer,
            multiplier=1,
            total_epoch=5,
            after_scheduler=scheduler)

    ## LOSS FUNCTION
    loss = get_loss_function(args)

    ## REPORT CALLBACK
    def report(epoch, esgd_dist=0, esgd_grad=0, lr_out=0, g_esgd=0):
        model.eval()
 
        o = dict() # store scalar observations
        oo = dict() # store array observations
        o["epoch"] = epoch
        oo["train_loss"], oo["train_error"], o["train_center_loss"], o["train_center_error"], _, o["norm_center"] = eval_loss_and_error(loss, model, train_loader)
        oo["test_loss"], oo["test_error"], o["test_center_loss"], o["test_center_error"], o["test_center_top5_error"], o["norm_center"] = eval_loss_and_error(loss, model, test_loader, train=False)

        o["dist_loss"] = model.distance_loss().item()
        o["coupl_loss"] = model.g * o["dist_loss"]
        
        oo["distances"] = np.sqrt(np.array([d.item()/model.num_params() for d in model.sqdistances()]))
        oo["norms"] = np.sqrt(np.array([sqn.item()/model.num_params() for sqn in model.sqnorms()]))
        o["gamma"] = model.g
        elif args.droplr:
            o["lr"] = lr_out
        else:
            o["lr"] = args.lr
            
        o['esgd_dist'] = esgd_dist
        o['esgd_grad'] = esgd_grad
        o['g_esgd'] = g_esgd

        print("\n", pd.DataFrame({k:[o[k]] for k in o}), "\n")
        for k in o:
            ex.log_scalar(k, o[k], epoch)
            if logdir and epoch > 0:
                writer.add_scalar(k, o[k], epoch)
        for k in oo:
            print(f"{k}:\t{oo[k]}")
            ex.log_scalar(k, np.mean(oo[k]), epoch) # Ref. https://github.com/IDSIA/sacred/issues/465
            if logdir and epoch > 0:
                writer.add_scalar(k, np.mean(oo[k]), epoch)
        print()
        return o, oo

    report(args.last_epoch)
    model.g *= args.gtime
    #model.g /= args.batch_size
    
    # dummy loop to make the schedulers progress
    for i in range(args.last_epoch):
        scheduler.step()
        if args.gsched == 'exp':
            model.increase_g()
        elif args.gsched == 'lin':
            model.increase_g_lin()
        elif args.gsched == 'cosine':
            model.increase_g_cosine()
    if args.gsched == 'cosine': # another one for cosine
        model.increase_g_cosine()
        
    print(f"# COUPLING SCHEDULE  g0: {model.g}  grate: {model.grate}")

    myfile = open("esgd_dists_%s_%s_%s_lr%s_g%s.dat"%(args.opt, args.model, args.dataset, args.lr, args.g), "w+", 1)
    mys = dict()
    ## START TRAINING
    for epoch in range(args.last_epoch + 1, args.epochs + 1):
        if args.opt == 'entropy-sgd':
            esgd_dist, esgd_grad, lr_out, g_esgd = train_esgd(loss, model, train_loader, optimizer, scheduler, y=args.y, g_time=args.gtime, file=myfile)
        elif args.opt == 'entropy-sgd2':
            esgd_dist, esgd_grad, lr_out, g_esgd = train_esgd2(loss, model, train_loader, optimizer, args, file=myfile, s=mys)
        if epoch % args.logtime == 0:
            report(epoch, esgd_dist=esgd_dist, esgd_grad=esgd_grad, lr_out=lr_out, g_esgd=g_esgd)
            
        # torch.cuda.empty_cache()
        if epoch % args.save_epoch == 0 and args.save_model:
            model_path = save_prefix+".pt"
            torch.save(model.state_dict(), model_path)
            if args.save_epoch > 0:
                kept_model_path = save_prefix+"_epoch_{}.pt".format(epoch)
                copyfile(model_path, kept_model_path)
                ex.add_artifact(kept_model_path, content_type="application/octet-stream")
            ex.add_artifact(model_path, content_type="application/octet-stream")
            
        #schedulers
        if args.gsched == 'exp':
            model.increase_g()
        elif args.gsched == 'lin':
            model.increase_g_lin()
        elif args.gsched == 'cosine':
            model.increase_g_cosine()

        if args.y == 1 and args.model.startswith('resnet110') and args.droplr != 'cosine': 
            # warm-up for ResNet-110 (single)
            mstones = [int(h) for h in args.drop_mstones.split('_')[1:]]
            if epoch == 1:
                print("warm-up for resnet-110: lr %s" % args.lr)
                args.lr = 0.1
                for param_group in optimizer.param_groups:
                    param_group['lr'] = args.lr
            elif epoch == mstones[0]:
                args.lr = 0.01
                for param_group in optimizer.param_groups:
                    param_group['lr'] = args.lr
            elif epoch == mstones[1]:
                args.lr = 0.001
                for param_group in optimizer.param_groups:
                    param_group['lr'] = args.lr
        else:
            if args.droplr:
                scheduler.step()
                print("lr=%s" % scheduler.get_lr())

    myfile.close()
    # Save model after training
    if args.save_model:
        model_path = save_prefix+"_final.pt"
        torch.save(model.state_dict(), model_path)
        ex.add_artifact(model_path, content_type="application/octet-stream")
