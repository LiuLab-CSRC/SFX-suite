#!/bin/env python

"""
Generate peak powder pattern on multiple cores using MPI.

Usage:
   batch_peak_powder.py <file-lst> <conf-file> [options]

Options:
    -h --help               Show this screen.
    -o FILE                 Specify output filename [default: powder.npz].
    --batch-size SIZE       Specify batch size in a job [default: 10].
    --buffer-size SIZE      Specify buffer size in MPI communication
                            [default: 500000].
    --flush                 Flush output of print.
"""
from mpi4py import MPI
import h5py
import numpy as np
import time

import sys
import os
from docopt import docopt
import yaml
import util


def master_run(args):
    flush = args['--flush']
    file_lst = args['<file-lst>']
    with open(file_lst) as f:
        _files = f.readlines()
    # remove trailing '/n'
    files = []
    for f in _files:
        if '\n' == f[-1]:
            files.append(f[:-1])
        else:
            files.append(f)
    # load hit finding configuration file
    with open(args['<conf-file>']) as f:
        conf = yaml.load(f)
    # collect jobs
    dataset = conf['dataset']
    batch_size = int(args['--batch-size'])
    buffer_size = int(args['--buffer-size'])
    jobs, nb_frames = util.collect_jobs(files, dataset, batch_size)
    nb_jobs = len(jobs)
    print('%d frames, %d jobs to be processed' %
          (nb_frames, nb_jobs), flush=flush)

    # dispatch jobs
    job_id = 0
    reqs = {}
    peaks = []
    slaves = set(range(1, size))
    finished_slaves = set()
    for slave in slaves:
        if job_id < nb_jobs:
            job = jobs[job_id]
        else:
            job = []  # dummy job
        comm.isend(job, dest=slave)
        reqs[slave] = comm.irecv(buf=buffer_size, source=slave)
        print('job %d/%d  --> %d' % (job_id, nb_jobs, slave), flush=flush)
        job_id += 1
    while job_id < nb_jobs:
        stop = False
        time.sleep(0.1)  # take a break
        slaves -= finished_slaves
        for slave in slaves:
            finished, result = reqs[slave].test()
            if finished:
                peaks += result
                if job_id < nb_jobs:
                    print('job %d/%d --> %d' %
                          (job_id, nb_jobs, slave), flush=flush)
                    comm.isend(stop, dest=slave)
                    comm.isend(jobs[job_id], dest=slave)
                    reqs[slave] = comm.irecv(buf=buffer_size, source=slave)
                    job_id += 1
                else:
                    stop = True
                    comm.isend(stop, dest=slave)
                    print('stop signal --> %d' % slave, flush=flush)
                    finished_slaves.add(slave)

    all_done = False
    while not all_done:
        all_done = True
        slaves -= finished_slaves
        for slave in slaves:
            finished, result = reqs[slave].test()
            if finished:
                peaks += result
                stop = True
                print('stop signal --> %d' % slave, flush=flush)
                comm.isend(stop, dest=slave)
                finished_slaves.add(slave)
            else:
                all_done = False

    # build and save peak powder
    filepath = jobs[0][0]['filepath']
    frame = jobs[0][0]['frame']
    h5_obj = h5py.File(filepath, 'r')
    image = util.read_image(
        filepath, frame=frame, h5_obj=h5_obj, dataset=dataset)
    powder = np.zeros(image.shape)
    peaks = np.round(np.array(peaks)).astype(np.int)
    powder[peaks[:, 0], peaks[:, 1]] = 1
    powder_file = args['-o']
    dir_ = os.path.dirname(powder_file)
    if not os.path.isdir(dir_):
        os.mkdir(dir_)
    np.savez(powder_file, powder_pattern=powder, powder_peaks=peaks)
    print('All Done!', flush=flush)
    MPI.Finalize()


def slave_run(args):
    stop = False
    filepath = None
    h5_obj = None
    buffer_size = int(args['--buffer-size'])
    flush = args['--flush']

    # hit finding parameters
    with open(args['<conf-file>']) as f:
        conf = yaml.load(f)
    gaussian_sigma = conf['gaussian filter sigma']
    mask_file = conf['mask file']
    if mask_file is not None:
        mask = util.read_image(mask_file)
    else:
        mask = None
    max_peak_num = conf['max peak num']
    min_distance = conf['min distance']
    min_gradient = conf['min gradient']
    min_snr = conf['min snr']
    dataset = conf['dataset']

    # perform hit finding
    while not stop:
        job = comm.recv(buf=buffer_size, source=0)
        peaks = []
        for i in range(len(job)):
            _filepath = job[i]['filepath']
            frame = job[i]['frame']
            if _filepath != filepath:
                filepath = _filepath
                h5_obj = h5py.File(filepath, 'r')
            image = util.read_image(filepath, frame=frame,
                               h5_obj=h5_obj, dataset=dataset)
            peaks_dict = util.find_peaks(
                image, mask=mask,
                gaussian_sigma=gaussian_sigma,
                min_distance=min_distance,
                min_gradient=min_gradient,
                max_peaks=max_peak_num,
                min_snr=min_snr
            )
            if peaks_dict['strong'] is not None:
                peaks += peaks_dict['strong'].tolist()
        comm.send(peaks, dest=0)
        stop = comm.recv(source=0)
        if stop:
            print('slave %d is exiting' % rank, flush=flush)


if __name__ == '__main__':
    comm = MPI.COMM_WORLD
    size = comm.Get_size()
    if size == 1:
        print('Run batch hit finder with at least 2 processes!')
        sys.exit()

    rank = comm.Get_rank()
    argv = docopt(__doc__)
    if rank == 0:
        master_run(argv)
    else:
        slave_run(argv)
