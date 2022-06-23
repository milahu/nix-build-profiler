#!/usr/bin/env python3

# print the cumulated cpu + memory usage of a process tree

# loosely based on https://gist.github.com/meganehouser/1752014
# license: MIT

# pip install psutil prefixed
# nix-shell -p python3 python3.pkgs.{psutil,prefixed}

# TODO detect idle workers: load < 0.1 over some time (5 seconds?)
# all workers can be idle in the first 1 or 2 seconds
# but then should produce cpu load 1 (or more)
# when cpu load drops back to zero, the worker should exit
# or the pool should kill the worker, and release the token

import psutil
from prefixed import Float

import time
import sys
import os
import shlex
import io

config_interval = 1
config_root_process_name = 'nix-daemon'

# debug: print env of every proc. verbose!
config_print_env_vars = False

#psutil.cpu_percent() # start monitoring cpu

cpu_count = psutil.cpu_count()
cpu_width = len(str(cpu_count * 100))
if cpu_width < len('load'):
  cpu_width = len('load')

# https://psutil.readthedocs.io/en/latest/#recipes
def find_procs_by_name(name):
  "Return a list of processes matching 'name'."
  ls = []
  for p in psutil.process_iter(['name']):
    if p.info['name'] == name:
      ls.append(p)
  return ls

def find_root_process(name):
  ls = find_procs_by_name(name)
  if len(ls) == 0:
    # return the first process
    # inside the nix-build sandbox, this is bash
    for p in psutil.process_iter():
      return p
  if len(ls) != 1:
    print(f"find_root_process: found multiple root procs:")
    for p in ls:
      print(f"  {p}")
    print(f"find_root_process: root_process = {ls[0]}")
  #assert len(ls) == 1 # !=1 when build is running
  return ls[0]


ps_fields = ['pid', 'ppid', 'name', 'exe', 'cmdline', 'cwd', 'environ', 'status', 'cpu_times', 'cpu_percent', 'memory_percent', 'memory_info', 'create_time']
# TODO num_threads?
# NOTE create_time not on windows

if config_print_env_vars:
  ps_fields.append('environ')

def init_process_info(process_info, pid):
  process_info[pid]["child_pids"] = list()
  process_info[pid]["sum_cpu"] = process_info[pid]["cpu_percent"]
  process_info[pid]["sum_mem"] = process_info[pid]["memory_percent"]
  process_info[pid]["sum_rss"] = process_info[pid]["memory_info"].rss
  # ncp = number of child processes
  # will be set in cumulate_process_info
  process_info[pid]["sum_ncp"] = 1 # 1 = include self
  # pretty
  if len(process_info[pid]["cmdline"]) == 0:
    process_info[pid]["cmdline"] = [os.path.basename(process_info[pid]["exe"])]
  else:
    # full path of info["cmdline"][0] is in info["exe"]
    process_info[pid]["cmdline"][0] = os.path.basename(process_info[pid]["cmdline"][0])
  process_info[pid]["total_time"] = time.time() - process_info[pid]["create_time"]
  process_info[pid]["alltime_load"] = (process_info[pid]["cpu_times"].user + process_info[pid]["cpu_times"].system) / process_info[pid]["total_time"]
  process_info[pid]["sum_alltime_load"] = process_info[pid]["alltime_load"]

def get_process_info(root_process):

  process_info = dict()

  found_root_process = False

  for process in psutil.process_iter(ps_fields):

    pid = process.info["pid"]

    # find start of tree
    if pid == root_process.pid:
      found_root_process = True
      process_info[pid] = process.info
      init_process_info(process_info, pid)
      continue

    if found_root_process == False:
      continue

    # exclude self
    if pid == os.getpid():
      continue

    # find children of tree
    ppid = process.info["ppid"]
    if ppid in process_info:
      process_info[pid] = process.info
      init_process_info(process_info, pid)
      process_info[ppid]["child_pids"].append(pid)

  return process_info


def cumulate_process_info(process_info, parent_pid):
  for child_pid in process_info[parent_pid]["child_pids"]:
    cumulate_process_info(process_info, child_pid) # depth first
    process_info[parent_pid]["sum_cpu"] += process_info[child_pid]["sum_cpu"]
    process_info[parent_pid]["sum_mem"] += process_info[child_pid]["sum_mem"]
    process_info[parent_pid]["sum_rss"] += process_info[child_pid]["sum_rss"]
    process_info[parent_pid]["sum_ncp"] += process_info[child_pid]["sum_ncp"]
    process_info[parent_pid]["sum_alltime_load"] += process_info[child_pid]["sum_alltime_load"]
  process_info[parent_pid]["ncp"] = len(process_info[parent_pid]["child_pids"])


todo_add_token_time = None

def print_process_info(
    process_info,
    root_pid,
    file=sys.stdout,
    depth=0,
    is_overload=False,
    is_underload=False,
    check_load=True,
    print_jobserver_stats=True,
  ):

  global todo_add_token_time

  # TODO rename root_pid to pid
  pid = root_pid

  if depth == 0:
    #print(f"\n{'load':<{cpu_width}s} mem rss  vms  proc @ {t}", file=file)
    #print(f"\n{'load':<{cpu_width}s} mem rss  Ncp ncp  proc @ {t}", file=file)
    #print(f"\n{'load':<{cpu_width}s}  rss spr cpr proc @ {t}", file=file)
    print(f"\n{'load':>{cpu_width}s} {'Load':>{cpu_width}s}  rss  time spr cpr proc", file=file)
    #print(f"\n{'load':<{cpu_width}s} mem proc @ {t}", file=file)
    # spr = sum of all child processes, including self
    # cpr = number of first child processes, excluding transitive children

  indent = "  "
  info = process_info[root_pid]
  sum_cpu = info["sum_cpu"] / 100 # = load
  sum_mem = info["sum_mem"]
  sum_rss = info["sum_rss"]
  sum_ncp = info["sum_ncp"]
  ncp = info["ncp"]
  name = info["name"]
  cmdline = info["cmdline"]
  # value None == psutil.AccessDenied
  exe = info["exe"] # always None
  cwd = info["cwd"] # always None
  environ = info["environ"] # always None
  child_procs = len(info["child_pids"])
  if len(cmdline) > 0:
    cmdline[0] = os.path.basename(cmdline[0]) # full path is in info["exe"]
  #if cmdline[0] in {"g++", "gcc"}:
  if cmdline[0] in {"g++", "gcc", "cc1plus", "as"}:
    # make gcc less verbose
    cmdline_short = []
    skip_value = False
    for arg in cmdline:
        if skip_value:
            skip_value = False
            continue
        if arg in {"-isystem", "-idirafter", "-D", "-I", "-MF", "-MMD"}:
            # -isystem is the most frequent
            skip_value = True
            continue
        if arg in {"-quiet", "-MQ"}:
            continue
        if arg[0:2] in {"-D", "-m", "-O", "-W", "-f"}:
            continue
        if arg.startswith("-std"):
            continue
        if arg.startswith("--param"):
            continue
        cmdline_short.append(arg)
    cmdline = cmdline_short

    #process_info[root_pid]["child_pids"] = [] # hide gcc child procs: cc1plus, as, ...
  log_info = {"child_procs": child_procs, "cmdline": cmdline, "cwd": cwd, "exe": exe}
  #if depth == 0:
  #  log_info["environ"] = environ # spammy
  print(f"{sum_cpu:{cpu_width}.1f} {sum_mem:3.0f} {Float(sum_rss):4.0h} {depth*indent}{name} info={repr(log_info)}", file=file)
  for child_pid in process_info[root_pid]["child_pids"]:
    print_process_info(
      process_info,
      child_pid,
      file,
      depth + 1,
      is_overload=is_overload,
      is_underload=is_underload,
      check_load=check_load,
      print_jobserver_stats=print_jobserver_stats,
    )


def main():

  root_process = find_root_process(config_root_process_name)

  max_load = int(os.environ.get("NIX_BUILD_CORES", "0"))
  min_load = 0.9 * max_load # 90% of 32 cores = 28.8
  total_cores = os.cpu_count()
  check_load = 0 < max_load and max_load < total_cores
  max_load_tolerance = 0.20 # 20%
  min_load_tolerance = 0
  tolerant_max_load = max_load * (1 + max_load_tolerance)
  tolerant_min_load = min_load * (1 - min_load_tolerance)

  check_load = False # debug. TODO expose option

  # mostly useless. should be "0 free tokens" for 99% of the build time.
  # at the end of the build, when tokens are missing, make prints a warning.
  # ninja should do the same (TODO implement?)
  print_jobserver_stats = True

  try:

    while True:

      process_info = get_process_info(root_process)

      cumulate_process_info(process_info, root_process.pid)

      string_file = io.StringIO()
      print_process_info(process_info, root_process.pid, file=string_file)
      print(string_file.getvalue(), end="") # one flush

      time.sleep(config_interval)

  except KeyboardInterrupt:
    sys.exit()

if __name__ == "__main__":

  main()
