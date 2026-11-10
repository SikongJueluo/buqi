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
        let mut output = std::io::stdout();
        self.serve_until(tokio::signal::ctrl_c(), DDS_OBSERVATION, &mut output)
            .await
    }

    async fn serve_until<F, W>(
        self,
        shutdown: F,
        observation_window: Duration,
        output: &mut W,
    ) -> Result<()>
    where
        F: Future<Output = std::io::Result<()>>,
        W: std::io::Write,
    {
        let Self {
            session,
            dds,
            locators: _,
        } = self;

        let mut dds = Some(dds);
        let mut dds_finished = false;
        let mut observed = false;
        let observation = tokio::time::sleep(observation_window);
        tokio::pin!(observation);
        tokio::pin!(shutdown);

        loop {
            tokio::select! {
                signal = &mut shutdown => {
                    signal.map_err(|error| anyhow!("等待关停信号失败：{error}"))?;
                    break;
                }
                // DDS 已经失败时不再把观察窗口当成“通过启动检查”。
                _ = &mut observation, if !observed && !dds_finished => {
                    observed = true;
                    let _ = writeln!(
                        output,
                        "DDS 插件已启动（观察 {} 秒内未报告失败；这不表示 DDS 已完成发现或机器人已连接）。",
                        observation_window.as_secs()
                    );
                }
                result = dds.as_mut().expect("DDS 任务未结束时句柄存在"), if !dds_finished => {
                    dds_finished = true;
                    let _ = writeln!(
                        output,
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
        let _ = writeln!(output, "网关已停止。");
        let _ = output.flush();
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

    async fn test_session() -> zenoh::Session {
        let mut config = zenoh::Config::default();
        config.insert_json5("mode", "\"peer\"").unwrap();
        config.insert_json5("listen/endpoints", "[]").unwrap();
        config
            .insert_json5("scouting/multicast/enabled", "false")
            .unwrap();
        config
            .insert_json5("scouting/gossip/enabled", "false")
            .unwrap();
        zenoh::open(config).await.expect("创建测试 Session 失败")
    }

    fn gateway_with(session: zenoh::Session, dds: JoinHandle<()>) -> Gateway {
        Gateway {
            session,
            dds,
            locators: Vec::new(),
        }
    }

    async fn serve_and_capture(
        gateway: Gateway,
        observation_window: Duration,
        shutdown_after: Duration,
    ) -> String {
        let mut output = Vec::new();
        gateway
            .serve_until(
                async move {
                    tokio::time::sleep(shutdown_after).await;
                    Ok(())
                },
                observation_window,
                &mut output,
            )
            .await
            .expect("前台守护失败");
        String::from_utf8(output).expect("输出不是 UTF-8")
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn failure_before_observation_is_not_reported_as_started() {
        let dds = tokio::spawn(async {
            tokio::time::sleep(Duration::from_millis(5)).await;
            panic!("模拟 DDS panic");
        });
        let output = serve_and_capture(
            gateway_with(test_session().await, dds),
            Duration::from_millis(50),
            Duration::from_millis(150),
        )
        .await;

        assert!(output.contains("DDS 插件不可用"), "{output}");
        assert!(
            !output.contains("DDS 插件已启动"),
            "DDS 失败后不得再输出通过观察：{output}"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn panic_after_observation_is_reported_after_started() {
        let dds = tokio::spawn(async {
            tokio::time::sleep(Duration::from_millis(80)).await;
            panic!("模拟 DDS panic");
        });
        let output = serve_and_capture(
            gateway_with(test_session().await, dds),
            Duration::from_millis(20),
            Duration::from_millis(250),
        )
        .await;

        let started = output.find("DDS 插件已启动").expect("观察窗口后应报告启动");
        let failed = output
            .find("DDS 插件不可用")
            .expect("panic 应被报告为不可用");
        assert!(started < failed, "顺序错误：{output}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn early_return_after_observation_is_reported() {
        let dds = tokio::spawn(async {
            tokio::time::sleep(Duration::from_millis(80)).await;
        });
        let output = serve_and_capture(
            gateway_with(test_session().await, dds),
            Duration::from_millis(20),
            Duration::from_millis(250),
        )
        .await;

        assert!(output.contains("运行期间返回"), "{output}");
        assert!(output.contains("DDS 插件不可用"), "{output}");
    }
}
