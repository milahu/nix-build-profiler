from setuptools import setup

with open('requirements.txt') as f:
    install_requires = f.read().splitlines()

setup(
  name='nix-build-profiler',
  version='0.0.1',
  author='Milan Hauth',
  description='profiling cpu and memory usage of nix-build',
  homepage='https://github.com/milahu/nix-build-profiler',
  install_requires=install_requires,
  entry_points={
    'console_scripts': ['nix-build-profiler=nix_build_profiler:main']
  },
)
