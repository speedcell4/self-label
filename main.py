import argparse
import numpy as np
import os
import time
import torch
import torch.nn as nn
import torch.optim
import torch.utils.data
import warnings

try:
    from tensorboardX import SummaryWriter
except ImportError:
    pass

import files
import util
import sinkhornknopp as sk
from data import return_model_loader

warnings.simplefilter("ignore", UserWarning)


class Trainer:
    def __init__(self, m, num_heads, num_clusters_per_head, train_loader, n_epochs, lr, weight_decay=1e-5,
                 ckpt_dir='/'):
        self.num_epochs = n_epochs
        self.lr = lr
        self.lr_schedule = lambda epoch: ((epoch < 350) * (self.lr * (0.1 ** (epoch // args.lrdrop)))
                                          + (epoch >= 350) * self.lr * 0.1 ** 3)

        self.momentum = 0.9
        self.weight_decay = weight_decay
        self.checkpoint_dir = ckpt_dir

        self.resume = True
        self.checkpoint_dir = None
        self.writer = None

        # model stuff
        self.num_heads = num_heads
        self.num_clusters_per_head = num_clusters_per_head
        self.model = m
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.nmodel_gpus = len(args.modeldevice)
        self.pseudo_loader = train_loader  # can also be DataLoader with less aug.
        self.train_loader = train_loader
        self.lamb = args.lamb  # the parameter lambda in the SK algorithm
        self.dtype = torch.float64 if not args.cpu else np.float64

        self.outs = [self.num_clusters_per_head] * args.hc
        # activations of previous to last layer to be saved if using multiple heads.
        self.presize = 4096 if args.arch == 'alexnet' else 2048

    def update_assignment(self, niter):
        if not args.cpu and torch.cuda.device_count() > 1:
            sk.gpu_sk(self)
        else:
            self.dtype = np.float64
            sk.cpu_sk(self)

        # save Label-assignments: optional
        # torch.save(self.L, os.path.join(self.checkpoint_dir, 'L', str(niter) + '_L.gz'))

        # free memory
        self.PS = 0

    def train_on_epoch(self, optimizer, loader, epoch, validation=False):
        print(f"Starting epoch {epoch}, validation: {validation} " + "=" * 30, flush=True)

        loss_value = util.AverageMeter()
        # house keeping
        self.model.run()
        if self.lr_schedule(epoch + 1) != self.lr_schedule(epoch):
            files.save_checkpoint_all(
                self.checkpoint_dir, self.model, args.arch,
                optimizer, self.L, epoch, lowest=False, save_str='pre-lr-drop')
        lr = self.lr_schedule(epoch)
        for pg in optimizer.param_groups:
            pg['lr'] = lr
        criterion_fn = torch.nn.CrossEntropyLoss()
        for index, (data, label, selected) in enumerate(loader):
            start_tm = time.time()
            global_step = epoch * len(loader) + index

            if global_step * args.batch_size >= self.optimize_times[-1]:
                # optimize labels #########################################
                self.model.headcount = 1
                print('Optimizaton starting', flush=True)
                with torch.no_grad():
                    _ = self.optimize_times.pop()
                    self.update_assignment(global_step)
            data = data.to(self.device)
            mass = data.size(0)
            outputs = self.model(data)
            # train CNN ####################################################
            if self.num_heads == 1:
                loss = criterion_fn(outputs, self.L[0, selected])
            else:
                loss = torch.mean(torch.stack([
                    criterion_fn(outputs[head_index], self.L[head_index, selected]) for head_index in
                    range(self.num_heads)]
                ))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            loss_value.update(loss.item(), mass)
            data = 0

            # some logging stuff ##############################################################
            if index % args.log_iter == 0 and self.writer:
                self.writer.add_scalar('lr', self.lr_schedule(epoch), global_step)

                print(global_step, f" Loss: {loss.item():.3f}", flush=True)
                print(global_step, f" Freq: {mass / (time.time() - start_tm):.2f}", flush=True)
                if writer:
                    self.writer.add_scalar('Loss', loss.item(), global_step)
                    if index > 0:
                        self.writer.add_scalar('Freq(Hz)', mass / (time.time() - start_tm), global_step)

        # end of epoch logging ################################################################
        if self.writer and (epoch % args.log_intv == 0):
            util.write_conv(self.writer, self.model, epoch=epoch)

        files.save_checkpoint_all(self.checkpoint_dir, self.model, args.arch, optimizer, self.L, epoch, lowest=False)

        return {'loss': loss_value.avg}

    def run(self):
        """Perform full optimization."""
        first_epoch = 0
        self.model = self.model.to(self.device)
        N = len(self.pseudo_loader.dataset)
        # optimization times (spread exponentially), can also just be linear in practice (i.e. every n-th epoch)
        self.optimize_times = [(self.num_epochs + 2) * N] + \
                              ((self.num_epochs + 1.01) * N * (np.linspace(0, 1, args.nopts) ** 2)[::-1]).tolist()

        sgd = torch.optim.SGD(filter(lambda p: p.requires_grad, self.model.parameters()),
                              weight_decay=self.weight_decay,
                              momentum=self.momentum,
                              lr=self.lr)

        if self.checkpoint_dir is not None and self.resume:
            self.L, first_epoch = files.load_checkpoint_all(self.checkpoint_dir, self.model, sgd)
            print('found first epoch to be', first_epoch, flush=True)
            include = [(qq / N >= first_epoch) for qq in self.optimize_times]
            self.optimize_times = (np.array(self.optimize_times)[include]).tolist()
        print('We will optimize L at epochs:', [np.round(1.0 * t / N, 2) for t in self.optimize_times], flush=True)

        if first_epoch == 0:
            # initiate labels as shuffled.
            self.L = np.zeros((self.num_heads, N), dtype=np.int32)
            for head_idex in range(self.num_heads):
                for index in range(N):
                    self.L[head_idex, index] = index % self.outs[head_idex]
                self.L[head_idex] = np.random.permutation(self.L[head_idex])
            self.L = torch.LongTensor(self.L).to(self.device)

        # Perform optmization ###############################################################
        lowest_loss = 1e9
        epoch = first_epoch
        while epoch < (self.num_epochs + 1):
            m = self.train_on_epoch(sgd, self.train_loader, epoch,
                                    validation=False)
            if m['loss'] < lowest_loss:
                lowest_loss = m['loss']
                files.save_checkpoint_all(self.checkpoint_dir, self.model, args.arch,
                                          sgd, self.L, epoch, lowest=True)
            epoch += 1
        print(f"optimization completed. Saving model to {os.path.join(self.checkpoint_dir, 'model_final.pth.tar')}")
        torch.save(self.model, os.path.join(self.checkpoint_dir, 'model_final.pth.tar'))
        return self.model


def build_argument_parser():
    parser = argparse.ArgumentParser(description='PyTorch Implementation of Self-Label')
    # optimizer
    parser.add_argument('--epochs', default=200, type=int, help='number of epochs')
    parser.add_argument('--batch-size', default=256, type=int, help='batch size (default: 256)')
    parser.add_argument('--lr', default=0.08, type=float, help='initial learning rate (default: 0.05)')
    parser.add_argument('--lrdrop', default=150, type=int, help='multiply LR by 0.1 every (default: 150 epochs)')
    parser.add_argument('--wd', default=-5, type=float, help='weight decay pow (default: (-5)')
    parser.add_argument('--dtype', default='f64', choices=['f64', 'f32'], type=str, help='SK-algo dtype (default: f64)')

    # SK algo
    parser.add_argument('--nopts', default=100, type=int, help='number of pseudo-opts (default: 100)')
    parser.add_argument('--augs', default=3, type=int, help='augmentation level (default: 3)')
    parser.add_argument('--lamb', default=25, type=int, help='for pseudoopt: lambda (default:25) ')
    parser.add_argument('--cpu', default=False, action='store_true', help='use CPU variant (slow) (default: off)')

    # architecture
    parser.add_argument('--arch', default='alexnet', type=str, help='alexnet or resnet (default: alexnet)')
    parser.add_argument('--archspec', default='big', choices=['big', 'small'], type=str,
                        help='alexnet variant (default:big)')
    parser.add_argument('--ncl', default=3000, type=int, help='number of clusters per head (default: 3000)')
    parser.add_argument('--hc', default=1, type=int, help='number of heads (default: 1)')

    # housekeeping
    parser.add_argument('--device', default='0', type=str, help='GPU devices to use for storage and model')
    parser.add_argument('--modeldevice', default='0', type=str, help='GPU numbers on which the CNN runs')
    parser.add_argument('--exp', default='self-label-default', help='path to experiment directory')
    parser.add_argument('--workers', default=6, type=int, help='number workers (default: 6)')
    parser.add_argument('--imagenet-path', default='', help='path to folder that contains `train` and `val`', type=str)
    parser.add_argument('--comment', default='self-label-default', type=str, help='name for tensorboardX')
    parser.add_argument('--log-intv', default=1, type=int, help='save stuff every x epochs (default: 1)')
    parser.add_argument('--log-iter', default=200, type=int, help='log every x-th batch (default: 200)')

    return parser.parse_args()


if __name__ == "__main__":
    args = build_argument_parser()
    name = f'{args.comment.replace("/", "_")}'
    try:
        args.device = [int(item) for item in args.device.split(',')]
    except AttributeError:
        args.device = [int(args.device)]
    args.modeldevice = args.device
    util.setup_runtime(seed=42, cuda_dev_id=list(np.unique(args.modeldevice + args.device)))
    print(f'args => {args}')
    print(f'name => {name}')
    time.sleep(5)

    writer = SummaryWriter(f'./runs/{name}')
    writer.add_text('args', " \n".join([f'{arg} {getattr(args, arg)}' for arg in vars(args)]))

    # Setup model and train_loader
    model, train_loader = return_model_loader(args)
    print(len(train_loader.dataset))
    model.to('cuda:0')
    if torch.cuda.device_count() > 1:
        print("Let's use", len(args.modeldevice), "GPUs for the model")
        if len(args.modeldevice) == 1:
            print('single GPU model', flush=True)
        else:
            model.features = nn.DataParallel(model.features,
                                             device_ids=list(range(len(args.modeldevice))))
    # Setup optimizer
    trainer = Trainer(m=model, num_heads=args.hc, num_clusters_per_head=args.ncl, train_loader=train_loader,
                      n_epochs=args.epochs, lr=args.lr, weight_decay=10 ** args.wd,
                      ckpt_dir=os.path.join(args.exp, 'checkpoints'))
    trainer.writer = writer
    # Optimize
    trainer.run()
