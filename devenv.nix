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

      # vibe code
      ".dsh/"
    ];
  };
}
