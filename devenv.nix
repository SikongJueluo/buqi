{
  pkgs,
  lib,
  config,
  ...
}: {
  # https://devenv.sh/packages/
  packages = with pkgs; [
    cargo-watch
    # cyclors（zenoh-plugin-dds 的 DDS 实现）构建内置 CycloneDDS C 源码需要 cmake，
    # 其 bindgen 生成 FFI 时需要 libclang。
    cmake
    libclang
    pkg-config
    # gstreamer crate（gstreamer-rs）包装系统 GStreamer；glib 供 glib-sys
    # 等 bindings 解析 glib-2.0.pc。
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    glib
    # Python 环境由 sdk/python 下的 uv 全权管理（uv 自带 Python 构建）。
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
