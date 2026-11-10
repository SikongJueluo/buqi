//! CLI 进程级检查：子命令可用，配置错误以非零退出码报告。

use std::process::Command;

#[test]
fn help_lists_the_server_command() {
    let output = Command::new(env!("CARGO_BIN_EXE_buqi"))
        .arg("--help")
        .output()
        .expect("运行 buqi --help 失败");

    assert!(output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("server"),
        "帮助信息应列出 server 子命令"
    );
}

#[test]
fn missing_config_file_exits_non_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_buqi"))
        .args(["server", "--config", "/nonexistent/buqi.toml"])
        .output()
        .expect("运行 buqi server 失败");

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("读取配置"),
        "错误信息应说明读取配置失败"
    );
}
