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
import subprocess

import gnumake_tokenpool

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
    #process_info[parent_pid]["sum_ncp"] += len(process_info[child_pid]["child_pids"])
  process_info[parent_pid]["ncp"] = len(process_info[parent_pid]["child_pids"])
  process_info[parent_pid]["sum_ncp"] += process_info[parent_pid]["ncp"]


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
    if cmdline[0] in {"g++", "gcc"}: # TODO fix other verbose commands
      # make gcc less verbose
      cmdline_short = []
      skip_value = False
      for arg in cmdline:
        if skip_value:
          skip_value = False
          continue
        if arg in {"-I", "-B", "-D", "-U", "-isystem", "-idirafter", "--param", "-MF", "-dumpdir", "-dumpbase", "-dumpbase-ext"}:
          # -isystem is the most frequent
          skip_value = True
          continue
        if arg in {"-pthread", "-pipe", "-MMD", "-MD", "-MT", "-quiet", "--64"}:
          continue
        if arg[0:2] in {"-I", "-B", "-D", "-U", "-m", "-O", "-W", "-f", "-g"}:
          continue
        if arg.startswith("-std="):
          continue
        if arg.startswith("--param="): # ex: --param=ssp-buffer-size=4
            continue
        cmdline_short.append(arg)
      cmdline = cmdline_short

    if cmdline[0] in {"g++", "gcc"}:
      # hide child procs
      process_info[root_pid]["child_pids"] = []
  # TODO print cwd only when different from parent process
  #log_info = {"cmdline": cmdline}
  log_info = {}
  if depth == 0:
    log_info["cwd"] = cwd
  else:
    parent_cwd = process_info[info["ppid"]]["cwd"]
    if cwd != parent_cwd:
      log_info["cwd"] = cwd
  #log_info["pid"] = pid
  log_info["exe"] = exe
  #if depth == 0:
  #  log_info["environ"] = environ # spammy
  info_str = ""
  cmdline_str = ""
  if len(cmdline) > 0 and cmdline[0] in {"g++", "gcc"}:
    cmdline_str = shlex.join(cmdline) # TODO rename name to cmdline_str
    #del log_info["cmdline"]
    #print(f"{sum_cpu:{cpu_width}.1f} {sum_mem:3.0f} {Float(sum_rss):4.0h} {depth*indent}{name} info={repr(log_info)}", file=file)
    # g++ has always 2 child procs: cc1plus, as
    # g++ has always the same cwd as its parent
  elif name in {"stress-ng"}:
    if process_info[info["ppid"]]["name"] == name:
      # fork
      pass
    else:
      # root process of stress-ng
      cmdline_str = shlex.join(cmdline) # TODO rename name to cmdline_str
  else:
    cmdline_str = shlex.join(cmdline) # print cmdline for all commands
    # FIXME rename name to cmdline_str
    if log_info:
      info_str = f" # {repr(log_info)}"

  mebi = 1024 * 1024

  # TODO fix name?
  if not name:
    name = exe
  name = os.path.basename(name)

  #total_time = float(info['total_time']) / 60.0 # time in minutes
  total_time = info['total_time'] # time in seconds

  # FIXME sum_ncp: off by one error
  #  load  Load  rss  time spr cpr proc
  #   1.1   0.1   17   0.2   5   2 bash 1: bash -e
  #   1.0   0.9    9   0.1   1   0   xz 13: xz -d
  #   0.1   0.1    2   0.1   1   0   tar 14: tar xf -
  # bash: spr should be 3 not 5

  #print(f"{sum_cpu:{cpu_width}.1f} {sum_mem:3.0f} {Float(sum_rss):4.0h} {sum_ncp:3d} {ncp:3d} {depth*indent}{name}{info_str}", file=file)
  #print(f"{sum_cpu:{cpu_width}.1f} {sum_ncp:3d} {Float(sum_rss):4.0h} {ncp:3d} {depth*indent}{name}{info_str}", file=file)
  #print(f"{sum_cpu:{cpu_width}.1f} {(sum_rss / mebi):4.0f} {sum_ncp:3d} {ncp:3d} {depth*indent}{name} {pid}: {cmdline_str}{info_str}", file=file)
  print(f"{sum_cpu:{cpu_width}.1f} {info['sum_alltime_load']:{cpu_width}.1f} {(sum_rss / mebi):4.0f} {total_time:5.0f} {sum_ncp:3d} {ncp:3d} {depth*indent}{name} {pid}: {cmdline_str}{info_str}", file=file)

  if config_print_env_vars:
    for k in info["environ"]:
      v = info["environ"][k]
      print(f"                   {depth*indent} {k}: {repr(v)}", file=file)

  # print extra info
  # here you can add custom code to debug your process tree
  debug_jobclient_fds = False
  if debug_jobclient_fds:
    # debug gnumake jobclient in jest-worker
    if (
      #name == "node" # error: name is the joined cmdline!
      #and
      len(cmdline) > 1
      and cmdline[1] == "../../../../../src/3rdparty/chromium/third_party/devtools-frontend/src/node_modules/rollup/dist/bin/rollup"
      # FIXME cmdline[1] can also be absolute path
      # /build/qtwebengine-everywhere-src-6.3.1/src/3rdparty/chromium/third_party/devtools-frontend/src/node_modules/rollup/dist/bin/rollup
      # -> use cmdline[1].endswith()
    ):
      # TODO summary: print parents of this proc. only names and pids
      # aaaaa 1
      #   bbbb 2
      #     cccccc 3
      #       ddddddd 4
      # loop from this process to all parents
      _pid = root_pid
      _depth = depth
      while _pid:
        _info = process_info[_pid]
        _cmdline_str = shlex.join(_info["cmdline"])
        print("", file=file)
        print(f"depth: {_depth}", file=file)
        print(f"proc {_pid}: {_cmdline_str}", file=file)
        print(f"env:", file=file)
        #for k in ["MAKEFLAGS", "DEBUG_JEST_WORKER", "DEBUG_JOBCLIENT"]:
        for k in ["MAKEFLAGS"]:
          v = _info["environ"].get(k)
          print(f"  {k}: {repr(v)}", file=file)
        # list file descriptors of process
        try:
          # TODO print only the jobserver fds, usually fd 3 and 4
          # parse fd numbers from MAKEFLAGS
          # TODO get fds from psutil? slow?
          cmd_str = f"ls -lv /proc/{_pid}/fd/"
          print(f"$ {cmd_str}", file=file)
          cmd_out = subprocess.check_output(cmd_str, shell=True, stderr=subprocess.STDOUT, text=True)
          file.write(cmd_out)
        except subprocess.CalledProcessError: # process is gone
          break # parents are gone too
        _pid = _info["ppid"]
        depth = depth - 1

  cmdline[0] = os.path.basename(cmdline[0]) # full path is in info["exe"]
  if print_jobserver_stats:
    if (
      #name == "node" # error: name is the joined cmdline!
      len(cmdline) > 2
      # ninja -j32 --tokenpool-master
      # ninja -j32 -l32 --tokenpool-master
      and cmdline[0] == "ninja"
      and "--tokenpool-master" in cmdline
    ):
      fds_str = None
      try:
        fds_str = os.listdir(f"/proc/{pid}/fd")
      # list of strings: ['0', '1', '2', '3', '17', '58', '59']
      except FileNotFoundError:
        pass
      if fds_str and '3' in fds_str and '4' in fds_str: # default fds: 3, 4
        num_tokens = None
        named_pipes = [
          f"/proc/{pid}/fd/3",
          f"/proc/{pid}/fd/4",
        ]
        jobclient = None
        try:
          jobclient = gnumake_tokenpool.JobClient(
            max_jobs=32, # TODO parse from cmdline: ninja -j32
            named_pipes=named_pipes,
            debug=False, debug2=False, # quiet
          )
        except gnumake_tokenpool.NoJobServer:
          pass
        if jobclient:
          free_tokens = None
          # acquire all tokens
          tokens = []
          while True:
            token = jobclient.acquire()
            if token is None:
              break
            tokens.append(token)
          #print(f"ninja jobserver: free tokens: {len(tokens)}", file=file)
          free_tokens = len(tokens)
          if free_tokens > 0:
            print(f"ninja jobserver: free tokens: {len(tokens)}", file=file)
          # TODO prettier ... with load ok, print only jobserver stats,
          # nothing of the process tree (except force_print==True or check_load==False)
          for token in tokens:
            jobclient.release(token)
          # workaround for bad jobclients who run idle and dont release their tokens
          # dont play a "zero sum game" but add new tokens to the game
          # risk: overload
          # the "idle" workers may generate cpu load in the future
          if free_tokens == 0 and is_underload:
            dt_add_token = 30 # add token every N seconds
            if todo_add_token_time == None:
              todo_add_token_time = time.time() + dt_add_token
              print(f"adding new token in {dt_add_token:.0f} seconds")
            else:
              todo_wait = todo_add_token_time - time.time()
              if todo_wait < 0:
                print(f"adding new token now")
                jobclient.release(43) # release default token 43
                todo_add_token_time = None # done
              else:
                print(f"adding new token in {todo_wait:.0f} seconds")
          elif todo_add_token_time != None:
            print(f"adding new token stopped. free_tokens={free_tokens} is_underload={is_underload}")
            todo_add_token_time = None # clear the todo
      if check_load and (is_overload == False and is_underload == False):
        # stop recursion -> short tree
        return

  # recursion
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

      total_load = process_info[root_process.pid]["sum_cpu"] / 100

      is_overload = total_load > tolerant_max_load
      is_underload = total_load < tolerant_min_load

      if print_jobserver_stats:
        check_load = False

      force_print = False
      if print_jobserver_stats:
        force_print = True
        # force print, at least print short tree until ninja + jobserver stats

      load_status_str = "ok"
      if is_overload:
        load_status_str = "overload"
      elif is_underload:
        load_status_str = "underload"

      # TODO print jobserver stats in the status_line
      t = time.strftime("%F %T %z")
      status_line = f"nix_build_profiler: load {total_load:.1f} = {load_status_str} @ {t}. min {min_load} ({tolerant_min_load:.1f}) max {max_load} ({tolerant_max_load:.1f})"

      if is_overload or is_underload or check_load == False or force_print == True:
        print("\n" + status_line)
      else:
        continue # dont print

      string_file = io.StringIO()
      print_process_info(
        process_info,
        root_process.pid,
        file=string_file,
        is_overload=is_overload,
        is_underload=is_underload,
        check_load=check_load,
        print_jobserver_stats=print_jobserver_stats,
      )
      print(string_file.getvalue(), end="") # one flush

      time.sleep(config_interval)

  except KeyboardInterrupt:
    sys.exit()

if __name__ == "__main__":

  main()
