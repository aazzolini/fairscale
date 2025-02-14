import logging
import math
import time

from golden_configs.lm_wikitext2 import MOE as MOEConfig
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP
import utils

MPI_PORT = 29500


def benchmark(config_class, args):
    """Benchmark a given model using a single process and multiple devices."""

    benchmark_config = utils.create_benchmark_config(args.model_name, config_class)
    model_specs = utils.get_model_specs(args.model_name, config_class)

    if not dist.is_initialized():
        logging.info(f"Running single process benchmark with args: {args}")
        # single process
        world_size = torch.cuda.device_count() if torch.cuda.is_available() else 1
        assert world_size > 0
        mp.spawn(train, args=(world_size, benchmark_config, model_specs, args), nprocs=world_size, join=True)
    else:
        # potentially multiple processes
        train(None, None, benchmark_config, model_specs, args)


def train(rank, world_size, benchmark_config, model_specs, args):
    logger = mp.log_to_stderr()
    logger.setLevel(logging.DEBUG if args.debug else logging.INFO)

    if not torch.distributed.is_initialized():
        init_method_pgroup = "tcp://localhost:{}".format(MPI_PORT)
        torch.distributed.init_process_group(
            backend="nccl", rank=rank, world_size=world_size, init_method=init_method_pgroup
        )

    rank = torch.distributed.get_rank()
    world_size = torch.distributed.get_world_size()

    utils.init_random_seed(rank)

    device = torch.device(
        "cuda",
        min(rank, torch.cuda.device_count() - 1)) if torch.cuda.is_available() else torch.device("cpu")
    
    logger.info("train, rank={}, device={}".format(rank, device))

    criterion = benchmark_config["criterion"]

    model_config = utils.create_model_config(
        args, benchmark_config=benchmark_config, model_specs=model_specs, device=device
    )
    # vocab_size may change in create_model_config() due to input data
    vocab_size = model_specs["vocab_size"]
    model = model_config["model"]
    model.train()
    optimizer = model_config["optimizer"]
    optimizer = optimizer(model.parameters())
    group = model.group if hasattr(model, "group") else None
    utils.log_number_of_parameters(model, logger)

    from dataclasses import dataclass

    @dataclass
    class Stats:
        total_loss = 0.0
        word_counter = 0
        total_tokens = 0
        total_tokens_per_log_interval = 0
        total_elapsed = 0.0
        bptt = 2

    stats = Stats()


    model = DDP(model, broadcast_buffers=False)  # , device_ids=[rank], output_device=rank, broadcast_buffers=False)
    lm_dataloader, _, _ = utils.get_data_loader(
        model_config["dataset_info"], args, benchmark_config, model_specs, num_replicas=world_size, rank=rank
    )

    def get_batch(source):
        seq_len = len(source) - 1
        data = source[0:seq_len]
        target = source[1 : 1 + seq_len]
        return data, target

    def train_some(num=None):
        epoch_start_time = 0

        num = num or args.max_batch
        for i, batch in enumerate(lm_dataloader):
            if i == 1:
                epoch_start_time = time.time()

            if args.max_batch and i > num:
                break

            if i > 0:
                stats.total_tokens += batch.numel()

            start_time = time.time()
            optimizer.zero_grad()
            source, target = get_batch(batch)
            source = source.to(device)
            target = target.to(device)
            try:
                output = model(source.to(device))
                loss = criterion(output.view(-1, vocab_size), target.view(-1))
                stats.total_loss += loss.item()
                loss.backward()
                torch.nn.utils.clip_grad_value_(model.parameters(), model_specs["clip_value"])
                optimizer.step()
            except Exception as e:
                raise RuntimeError(f"training failed on {torch.distributed.get_rank()}") from e

            elapsed = time.time() - start_time
            stats.total_elapsed += elapsed
            log_interval = 1
            stats.total_tokens_per_log_interval += batch.numel()
            if i % log_interval == 0 and i > 0:
                cur_loss = stats.total_loss / log_interval
                logger.debug(
                    "| batch {:5d} | wps {:5.2f} | loss {:5.2f} | ppl {:8.2f}".format(
                        i, stats.total_tokens_per_log_interval / elapsed, cur_loss, math.exp(cur_loss)
                    )
                )
                stats.total_tokens_per_log_interval = 0
                stats.total_loss = 0

    train_some()

    with torch.profiler.profile(
        activities=[torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA],
        # schedule=torch.profiler.schedule(wait=2, warmup=2, active=6, repeat=1),
        # on_trace_ready=tensorboard_trace_handler,
        # with_stack=True
    ) as profiler:
        train_some(num=2)

    profiler.export_chrome_trace(f'trace_{rank}.json')

    wps = stats.total_tokens / stats.total_elapsed

    device_id = 0
    logger.debug("rank {}, wps: {}".format(rank, wps))
    logger.debug(
        "Peak allocated bytes on cuda:{}: {:1d}".format(
            dist.get_rank(), torch.cuda.memory_stats(device_id)["allocated_bytes.all.peak"]
        )
    )


def main():
    args = utils.init_args()
    logging.basicConfig(level=logging.INFO if not args.debug else logging.DEBUG)

    benchmark(MOEConfig, args)


if __name__ == "__main__":
    main()
