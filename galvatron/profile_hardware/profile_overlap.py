import torch
from torch import nn
import argparse
import os
import json

def read_json_config(path):
    return json.load(open(path,'r',encoding="utf-8"))

def write_json_config(config, path):
    with open(path,'w') as fp:
        json.dump(config,fp, indent=4)

def profile(args):
    torch.distributed.init_process_group(backend="nccl")
    local_rank = args.local_rank
    rank = torch.distributed.get_rank()
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    world_size = torch.distributed.get_world_size()

    model = nn.Linear(4096, 4096, bias=False).cuda()
    compute_tensor = torch.randn((1024,4096), device=device)
    comm_tensor = torch.randn((4096,4096), device=device)

    comm_stream = torch.cuda.Stream()
    compute_stream = torch.cuda.current_stream()
    torch.cuda.Stream.synchronize(compute_stream)
    comm_time_list = []
    compute_time_list = []
    
    def compute_func(dummy_input, iters):
        with torch.cuda.stream(compute_stream):
            for i in range(iters):
                output = model(compute_tensor)
    
    def comm_func(dummy_input, iters):
        with torch.cuda.stream(comm_stream):
            for i in range(iters):
                torch.distributed.all_reduce(comm_tensor)
                
    def compute_comm_func(dummy_input, compute_iters, comm_iters):
        comm_func(dummy_input, comm_iters)
        compute_func(dummy_input, compute_iters)

    def str2time(s):
        if 'ms' in s:
            return float(s[:-2])
        elif 'us' in s:
            return float(s[:-2])*1e-3
        else:
            return float(s[:-1])*1e3
    
    def average_op_time(op_str, cuda_total_idx, call_times_idx):
        if op_str is None:
            return None
        op_time = str2time(op_str[cuda_total_idx])/int(op_str[call_times_idx])
        op_time = torch.tensor([op_time]).to(device)
        torch.distributed.all_reduce(op_time, op=torch.distributed.ReduceOp.SUM)
        op_time = op_time.cpu().numpy()[0] / world_size
        return op_time

    def split_line(line):
        line = line.split('  ')
        ls = []
        for s in line:
            if len(s):
                ls.append(s.strip())
        return ls

    def trace_handler(prof):
        if local_rank > -1:
            table = prof.key_averages().table(sort_by="self_cuda_time_total", row_limit=5)
            if local_rank == 0:
                print(table)
            table = table.split('\n')
            comm_str, compute_str = None, None
            for line in table:
                if 'Name' in line:
                    title = split_line(line)
                if 'ncclKernel_AllReduce' in line:
                    comm_str = split_line(line)
                if 'gemm' in line:
                    compute_str = split_line(line)
            for i in range(len(title)):
                if 'CUDA total' in title[i]:
                    cuda_total_idx = i
                if '# of Calls' in title[i]:
                    call_times_idx = i
            comm_time, compute_time = None, None
            comm_time = average_op_time(comm_str, cuda_total_idx, call_times_idx)
            compute_time = average_op_time(compute_str, cuda_total_idx, call_times_idx)
            if comm_time is not None:
                if local_rank == 0:
                    print('Average communication time (ms):', comm_time)
                comm_time_list.append(comm_time)
                # print('rank %d, comm_time %.3f'%(rank, comm_time))
            if compute_time is not None:
                if local_rank == 0:
                    print('Average computation time (ms):', compute_time)
                compute_time_list.append(compute_time)
                # print('rank %d, compute_time %.3f'%(rank, compute_time))

    def profile_op(sync_stream, warmup_func, profile_func):
        with torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CUDA],
                                schedule=torch.profiler.schedule(wait=0,warmup=1,active=1),
                                on_trace_ready=trace_handler) as p:
            for i in range(2):
                if rank == 0:
                    if i == 0:
                        print('Warming up...')
                    else:
                        print('Profiling...')
                dummy_input = None
                if i == 0:
                    warmup_func(dummy_input)
                else:
                    profile_func(dummy_input)
                torch.cuda.Stream.synchronize(sync_stream)
                p.step()

    if rank == 0:
        print('Profiling computation time when not overlapped with communication...')
    profile_op(compute_stream, lambda x: compute_func(x, 512), lambda x: compute_func(x, 512))
        
    if rank == 0:
        print('Profiling communication time when not overlapped with computation...')
    profile_op(comm_stream, lambda x: comm_func(x, 10), lambda x: comm_func(x, 30))

    overlap_time_multiply = 4
    
    # computation overlaps communication
    if rank == 0:
        print('\nProfiling communication time when overlapped with computation...')
    comm_iters = max(int(1000 / comm_time_list[0]), 5) # 1000 ms for communication
    compute_iters = int(overlap_time_multiply*comm_iters*comm_time_list[0]/compute_time_list[0])
    profile_op(comm_stream, lambda x: comm_func(x, comm_iters), lambda x: compute_comm_func(x, compute_iters, comm_iters))
    comm_delay = comm_time_list[1] / comm_time_list[0]

    # communication overlaps computation
    if rank == 0:
        print('\nProfiling communication time when overlapped with computation...')
    compute_iters = max(int(1000 / compute_time_list[0]), 5) # 1000 ms for computation
    comm_iters = int(overlap_time_multiply*compute_iters*compute_time_list[0]/comm_time_list[0])
    profile_op(compute_stream, lambda x: comm_func(x, comm_iters), lambda x: compute_comm_func(x, compute_iters, comm_iters))
    compute_delay = compute_time_list[2] / compute_time_list[0]

    overlap_coe = max(comm_delay, compute_delay)

    if local_rank == 0:
        print('comm_times:', comm_time_list)
        print('compute_times:', compute_time_list)
        print('overlap_coe:', overlap_coe)
        path = os.path.dirname(os.path.abspath(__file__))
        env_config_path = os.path.join(path, './hardware_configs/overlap_coefficient.json')
        config = read_json_config(env_config_path) if os.path.exists(env_config_path) else dict()
        key = 'overlap_coe'
        overlap_coe = overlap_coe if overlap_coe > 1.0 else 1.0
        config[key] = overlap_coe
        print('\n********************')
        print('Overlap coefficient:', config[key])
        write_json_config(config, env_config_path)
        print('Already written overlap_coefficient into env config file %s!'%(env_config_path))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-rank" ,type=int,default=-1)
    parser.add_argument("--overlap_time_multiply", type=int, default=4, help='The multiple of communication time and computation time when overlapped.')
    args = parser.parse_args()
    profile(args)