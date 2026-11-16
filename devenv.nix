{
  pkgs,
  lib,
  config,
  ...
}:
let
  # nixpkgs 默认不构建 plugins-rs 的 webrtc 插件（其测试失败被排除）。
  # 这里沿用同一份 nixpkgs 源码，只构建 webrtc 插件以提供 webrtcsink。
  gstPluginsRsWebrtc =
    (pkgs.gst_all_1.gst-plugins-rs.override {
      # meson 的 webrtc 选项依赖 rtp 插件，必须一起启用。
      plugins = [ "webrtc" "rtp" ];
    }).overrideAttrs {
      doCheck = false;
    };
in {
  # https://devenv.sh/packages/
  packages = with pkgs; [
    cargo-watch
    # cyclors（zenoh-plugin-dds 的 DDS 实现）构建内置 CycloneDDS C 源码需要 cmake，
    # 其 bindgen 生成 FFI 时需要 libclang。
    cmake
    libclang
    pkg-config
    # workflow
    act
    # gstreamer crate（gstreamer-rs）包装系统 GStreamer；glib 供 glib-sys
    # 等 bindings 解析 glib-2.0.pc。
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    # v4l2src、jpegenc/jpegdec 与 RTP 负载化属于 plugins-good；
    # 相机 MJPEG 直通与 WebRTC 分支都需要它。
    gst_all_1.gst-plugins-good
    # webrtcbin 与 ICE 等 WebRTC 基础设施；webrtcsink 的运行时依赖。
    gst_all_1.gst-plugins-bad
    # x264enc 仅作为开发机显式选择的软件 H.264 后端，不自动回退。
    gst_all_1.gst-plugins-ugly
    # 自定义构建：提供 webrtcsink/webrtcsrc（默认 nixpkgs 包不含）。
    gstPluginsRsWebrtc
    glib
    # Python 环境由根目录 uv 项目（pyproject.toml/uv.lock）管理（uv 自带 Python 构建）。
    uv
  ];

  # bindgen 通过 LIBCLANG_PATH 定位 libclang.so（NixOS 非标准路径）。
  env.LIBCLANG_PATH = "${pkgs.libclang.lib}/lib";

  # 无包装的 libclang 不带 NixOS 的系统头文件搜索路径，bindgen 解析
  # wrapper.h 时需要补上 clang 内置头（资源目录）与 glibc 头。
  enterShell = ''
    if command -v clang >/dev/null 2>&1; then
      export BINDGEN_EXTRA_CLANG_ARGS="''${BINDGEN_EXTRA_CLANG_ARGS:-} -isystem $(clang -print-resource-dir)/include -isystem ${lib.getDev pkgs.glibc}/include"
    fi

    # using podman with act
    export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
  '';

  # https://devenv.sh/languages/
  languages = {
    nix.enable = true;
    rust = {
      enable = true;
      lsp.enable = true;
    };
  };

  integrations.gitnr.".gitignore" = {
    templates = [
      "gh:Rust"
      "gh:Nix"
    ];

    content = [
      # Devenv
      ".devenv*"
      "devenv.local.nix"
      "devenv.local.yaml"

      # direnv
      ".direnv"

      # pre-commit
      ".pre-commit-config.yaml"

      # others
      ".env"
      "/target/"
      "**/target/"
      ".venv"
      "__pycache__/"
      "*.pyc"

      # vibe code
      ".dsh/"
    ];
  };
}
