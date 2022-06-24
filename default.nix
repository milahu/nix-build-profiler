{ lib
, python3
, writeText
}:

let
  inherit (python3.pkgs) buildPythonPackage psutil prefixed;
in

buildPythonPackage rec {
  pname = "nix-build-profiler";
  version = "0.0.1";

  src = ./.;

  propagatedBuildInputs = [
    psutil
    prefixed
  ];

  setupHook = writeText "setup-hook.sh" ''
    startNixBuildProfiler() {
      echo "Starting nix-build-profiler"
      nix-build-profiler &
    }
    prePhases+=" startNixBuildProfiler"
  '';

  meta = with lib; {
    description = "Profile CPU and memory usage of nix-build";
    longDescription = ''
      Usage:

      ```nix
      mkDerivation {
        nativeBuildInputs = [ nix-build-profiler ];
      }
      ```
    '';
    homepage = "https://github.com/milahu/nix-build-profiler";
    license = licenses.mit;
    maintainers = with maintainers; [ milahu ];
  };
}
