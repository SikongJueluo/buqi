# 网关容器镜像：把本次构建的 buqi 二进制及其 Nix 运行库闭包打成可被 Podman 加载的镜像。
#
# 只用于多容器模拟通信验收。nixpkgs 取自 devenv.lock 锁定的版本，和开发环境一致；
# 镜像内是完整的 /nix/store 闭包，因此不是把 Nix 编译的 ELF 裸复制进普通 Linux 镜像。
#
# 二进制是在 devenv 里编译的，rpath 指向具体包的 /nix/store 路径。Nix 只把派生声明的
# 输入算进引用闭包，所以这里显式列出运行库包（同一 nixpkgs 锁定版本，路径与 rpath 一致）。
# devenv-shell-env 是开发环境的聚合路径，不属于运行闭包；缺失时动态加载器会跳过该目录。
#
# 用法（在仓库根目录，先 cargo build --workspace）：
#   nix build --impure --file containers/gateway/image.nix --out-link target/acceptance/gateway-image
# 然后用 podman load -i target/acceptance/gateway-image 加载。
let
  lock = builtins.fromJSON (builtins.readFile ../../devenv.lock);
  pinned = lock.nodes.nixpkgs-src.locked;
  nixpkgs = builtins.fetchTarball {
    url = "https://github.com/${pinned.owner}/${pinned.repo}/archive/${pinned.rev}.tar.gz";
    sha256 = pinned.narHash;
  };
  pkgs = import nixpkgs {};

  runtimeLibs = with pkgs; [
    (lib.getLib glib)
    (lib.getLib gst_all_1.gstreamer)
    (lib.getLib gst_all_1.gst-plugins-base)
  ];

  gateway = pkgs.runCommandLocal "buqi-gateway-bin" {nativeBuildInputs = [pkgs.binutils];} ''
    install -Dm755 ${../../target/debug/buqi} $out/bin/buqi
    strip --strip-debug $out/bin/buqi
  '';

  env = pkgs.buildEnv {
    name = "buqi-gateway-env";
    paths = [gateway] ++ runtimeLibs;
    pathsToLink = ["/bin" "/lib" "/lib64" "/share/gst-plugins" "/lib/gstreamer-1.0"];
  };
in
pkgs.dockerTools.buildLayeredImage {
  name = "buqi-gateway";
  tag = "local";
  contents = [
    pkgs.dockerTools.binSh
    pkgs.coreutils
    env
  ];
  config = {
    Entrypoint = ["/bin/buqi"];
    WorkingDir = "/";
  };
}
