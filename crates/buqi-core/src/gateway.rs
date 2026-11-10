//! 网关启动、监听报告与 DDS 主任务监督。
//!
//! 网关始终是 Zenoh Router：手动构建 runtime（与 `zenoh::open` 相同路径），
//! 再以可观测的 Tokio 任务启动 `zenoh-plugin-dds` 的公开 `run` 入口。
//! 这样 DDS 任务的提前返回或 panic 能被观察到，而不必复制插件实现。
//! 插件本身没有 ready/清理协议，因此这里只承诺“任务在运行”，不承诺
//! DDS 完成发现、机器人已连接或底层数据路径全部被隔离。

use std::future::Future;
use std::path::Path;
use std::time::Duration;

use anyhow::{Result, anyhow};
use tokio::task::{JoinError, JoinHandle};
use zenoh::config::Locator;
use zenoh::internal::runtime::RuntimeBuilder;

use crate::config::{GatewayConfig, ListenSource};

/// DDS 主任务观察窗口。
///
/// 只用于发现启动后立刻退出或 panic 的任务，不是 DDS ready 信号。
const DDS_OBSERVATION: Duration = Duration::from_secs(2);

/// 已启动、尚未关闭的网关。
pub struct Gateway {
    session: zenoh::Session,
    dds: JoinHandle<()>,
    locators: Vec<Locator>,
}

/// 前台运行网关，直到收到 Ctrl+C。
pub async fn run(config_path: Option<&Path>) -> Result<()> {
    Gateway::start(config_path).await?.serve().await
}

impl Gateway {
    /// 启动 Zenoh Router 与 DDS 插件任务。
    ///
    /// 监听失败（端口占用、地址不可用）会返回错误；DDS 插件的异步失败
    /// 不阻止 Router 启动，由 [`Gateway::serve`] 报告。
    pub async fn start(config_path: Option<&Path>) -> Result<Self> {
        zenoh::try_init_log_from_env();

        let (zenoh_config, dds_config, listen_source) =
            GatewayConfig::load(config_path)?.into_parts();

        let mut runtime = RuntimeBuilder::new(zenoh_config)
            .build()
            .await
            .map_err(|error| anyhow!("初始化 Zenoh runtime 失败：{error}"))?;
        let session = zenoh::session::init(runtime.clone().into())
            .await
            .map_err(|error| anyhow!("创建 Zenoh Session 失败：{error}"))?;
        runtime.start().await.map_err(|error| {
            anyhow!(
                "启动 Zenoh 监听失败：{error}；端口可能被占用或监听地址不可用，请检查配置或释放端口"
            )
        })?;

        let locators = runtime.get_locators();
        report_listeners(&locators, listen_source);

        let dds = tokio::spawn(zenoh_plugin_dds::run(runtime.into(), dds_config));

        Ok(Self {
            session,
            dds,
            locators,
        })
    }

    /// 实际监听的 Zenoh 端点，例如 `tcp/127.0.0.1:7447`。
    pub fn locators(&self) -> Vec<String> {
        self.locators.iter().map(ToString::to_string).collect()
    }

    /// 前台守护：在观察窗口后报告 DDS 状态，直到 Ctrl+C 或 DDS 任务退出。
    ///
    /// 无论 DDS 是否可用，Router 都保持运行；DDS 失败只发警告，不重启。
    pub async fn serve(self) -> Result<()> {
        self.serve_until(tokio::signal::ctrl_c()).await
    }

    async fn serve_until<F>(self, shutdown: F) -> Result<()>
    where
        F: Future<Output = std::io::Result<()>>,
    {
        let Self {
            session,
            dds,
            locators: _,
        } = self;

        let mut dds = Some(dds);
        let mut dds_finished = false;
        let mut observed = false;
        let observation = tokio::time::sleep(DDS_OBSERVATION);
        tokio::pin!(observation);
        tokio::pin!(shutdown);

        loop {
            tokio::select! {
                signal = &mut shutdown => {
                    signal.map_err(|error| anyhow!("等待关停信号失败：{error}"))?;
                    break;
                }
                _ = &mut observation, if !observed => {
                    observed = true;
                    println!(
                        "DDS 插件已启动（观察 {} 秒内未报告失败；这不表示 DDS 已完成发现或机器人已连接）。",
                        DDS_OBSERVATION.as_secs()
                    );
                }
                result = dds.as_mut().expect("DDS 任务未结束时句柄存在"), if !dds_finished => {
                    dds_finished = true;
                    println!(
                        "警告：DDS 插件不可用（{}）。Zenoh Router 继续运行；修复原因后请重启网关。",
                        dds_exit_reason(result)
                    );
                }
            }
        }

        if let Some(handle) = dds
            && !dds_finished
        {
            // 插件没有关闭协议：取消任务后由进程退出释放 DDS 原生资源。
            handle.abort();
            let _ = handle.await;
        }
        session
            .close()
            .await
            .map_err(|error| anyhow!("关闭 Zenoh Session 失败：{error}"))?;
        println!("网关已停止。");
        Ok(())
    }
}

fn report_listeners(locators: &[Locator], listen_source: ListenSource) {
    println!("Buqi 网关已启动（Zenoh Router + DDS 插件桥接）");
    if locators.is_empty() {
        println!("当前没有监听端点。");
    } else {
        println!("客户端连接端点（可直接用于 Zenoh）：");
        for locator in locators {
            println!("  {locator}");
        }
    }
    if let ListenSource::Automatic { lan_addresses: 0 } = listen_source {
        println!("提示：未找到符合条件的局域网 IPv4 地址，仅在本机回环地址上监听。");
    }
    println!("以上仅表示本机已监听，不保证其他主机可达；网络地址变化后需要重启网关。");
}

fn dds_exit_reason(result: std::result::Result<(), JoinError>) -> String {
    match result {
        Ok(()) => "主任务在运行期间返回".to_owned(),
        Err(error) if error.is_panic() => "主任务发生 panic".to_owned(),
        Err(error) => format!("主任务被取消（{error}）"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> std::path::PathBuf {
        let path =
            std::env::temp_dir().join(format!("buqi-gateway-test-{}.toml", std::process::id()));
        std::fs::write(
            &path,
            "listen = { endpoints = [\"tcp/127.0.0.1:0\"] }\n\n\
             [plugins.dds]\ndomain = 199\nlocalhost_only = true\n",
        )
        .expect("写入测试配置失败");
        path
    }

    #[tokio::test]
    async fn panic_in_supervised_task_is_reported() {
        let handle = tokio::spawn(async { panic!("模拟 DDS panic") });
        let reason = dds_exit_reason(handle.await);
        assert!(reason.contains("panic"), "{reason}");
    }

    #[tokio::test]
    async fn task_returning_early_is_reported() {
        let handle = tokio::spawn(async {});
        let reason = dds_exit_reason(handle.await);
        assert!(reason.contains("返回"), "{reason}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dds_panic_keeps_router_running_until_shutdown() {
        let config_path = test_config();
        let mut gateway = Gateway::start(Some(&config_path))
            .await
            .expect("启动网关失败");
        std::fs::remove_file(&config_path).ok();

        gateway.dds = tokio::spawn(async { panic!("模拟 DDS panic") });

        let result = gateway
            .serve_until(async {
                tokio::time::sleep(Duration::from_millis(100)).await;
                Ok(())
            })
            .await;

        assert!(result.is_ok(), "DDS panic 不应终止网关：{result:?}");
    }
}
