//! 网关配置：原生 Zenoh TOML 配置格式，加上 Buqi 的启动契约。
//!
//! 配置中的 `listen.endpoints` 用字符串数组写法：
//!
//! - 省略或空数组：自动选择本机私有 IPv4 与 `127.0.0.1`，端口 7447；
//! - 非空数组：完整替换自动选择，不再追加回环地址。
//!
//! `plugins.dds` 沿用 `zenoh-plugin-dds` 的字段，交给网关监督启动；
//! `mode`、scouting 与 `listen.endpoints` 由 Buqi 固定，配置中的对应值会被覆盖。

use std::net::Ipv4Addr;
use std::path::Path;

use anyhow::{Context, Result, anyhow, bail};
use serde_json::Value;
use zenoh::config::EndPoint;
use zenoh_plugin_dds::config::Config as DdsConfig;

use crate::net;

/// 自动选择监听地址时使用的固定 TCP 端口。
pub const DEFAULT_PORT: u16 = 7447;

/// 监听端点的来源，用于启动后说明客户端的连接范围。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListenSource {
    /// 自动选择：给定数量的局域网地址，加上回环地址。
    Automatic { lan_addresses: usize },
    /// 配置中的 `listen.endpoints` 完整替换自动选择。
    Configured,
}

/// 解析完成、可以直接交给网关启动的配置。
pub struct GatewayConfig {
    zenoh: zenoh::Config,
    dds: DdsConfig,
    listen_source: ListenSource,
}

impl GatewayConfig {
    /// 读取 TOML 配置；`None` 表示不使用配置文件。
    ///
    /// 自动选择只在配置未给出监听端点时才读取本机接口。枚举失败会作为
    /// 启动错误返回，不会退化成“只有回环地址”。
    pub fn load(path: Option<&Path>) -> Result<Self> {
        let zenoh = match path {
            Some(path) => zenoh::Config::from_file(path)
                .map_err(|error| anyhow!("读取配置 {} 失败：{error}", path.display()))?,
            None => zenoh::Config::default(),
        };
        Self::from_zenoh(zenoh, || {
            let interfaces = net::snapshot()?;
            Ok(net::lan_addresses(&interfaces))
        })
    }

    fn from_zenoh(
        mut zenoh: zenoh::Config,
        lan_addresses: impl FnOnce() -> std::io::Result<Vec<Ipv4Addr>>,
    ) -> Result<Self> {
        let dds = take_dds_config(&mut zenoh)?;
        let (endpoints, listen_source) = match read_configured_endpoints(&zenoh)? {
            Some(endpoints) => (endpoints, ListenSource::Configured),
            None => {
                let lan_addresses = lan_addresses().context("枚举本机网络接口失败")?;
                let endpoints = automatic_endpoints(&lan_addresses);
                (
                    endpoints,
                    ListenSource::Automatic {
                        lan_addresses: lan_addresses.len(),
                    },
                )
            }
        };

        // Buqi 固定以 router 角色运行；客户端使用显式 locator，关闭 scouting。
        zenoh
            .insert_json5("mode", "\"router\"")
            .map_err(|error| anyhow!("设置 router 模式失败：{error}"))?;
        zenoh
            .insert_json5("scouting/multicast/enabled", "false")
            .map_err(|error| anyhow!("关闭多播 scouting 失败：{error}"))?;
        zenoh
            .insert_json5("scouting/gossip/enabled", "false")
            .map_err(|error| anyhow!("关闭 gossip scouting 失败：{error}"))?;
        // 绑定失败必须让启动失败：固定“不等待、不后台重试、失败即退出”。
        // 端点级重试参数在读取监听端点时拒绝。
        zenoh
            .insert_json5("listen/timeout_ms", "0")
            .map_err(|error| anyhow!("固定监听超时失败：{error}"))?;
        zenoh
            .insert_json5("listen/exit_on_failure", "true")
            .map_err(|error| anyhow!("固定监听失败行为失败：{error}"))?;
        zenoh
            .insert_json5("listen/endpoints", &endpoints_json(&endpoints))
            .map_err(|error| anyhow!("设置监听端点失败：{error}"))?;

        Ok(Self {
            zenoh,
            dds,
            listen_source,
        })
    }

    pub(crate) fn into_parts(self) -> (zenoh::Config, DdsConfig, ListenSource) {
        (self.zenoh, self.dds, self.listen_source)
    }
}

/// 取出并移除 `plugins.dds`。
///
/// 插件由网关自己启动并监督，不能再留给 Zenoh 的插件加载器启动，
/// 否则会出现第二个未经监督的 DDS 参与者。
fn take_dds_config(zenoh: &mut zenoh::Config) -> Result<DdsConfig> {
    let plugins_json = zenoh
        .get_json("plugins")
        .map_err(|error| anyhow!("读取 plugins 配置失败：{error}"))?;
    let plugins: Value =
        serde_json::from_str(&plugins_json).context("plugins 配置不是合法 JSON")?;
    let Value::Object(plugins) = plugins else {
        bail!("plugins 配置必须是表");
    };

    if let Some(name) = plugins.keys().find(|name| *name != "dds") {
        bail!("仅支持 plugins.dds 配置，发现 plugins.{name}");
    }

    let Some(dds) = plugins.get("dds") else {
        return serde_json::from_str("{}").context("DDS 插件默认配置无效");
    };

    let dds = serde_json::from_value::<DdsConfig>(dds.clone())
        .with_context(|| format!("plugins.dds 配置无效：{dds}"))?;
    zenoh
        .remove("plugins/dds")
        .map_err(|error| anyhow!("移除 plugins.dds 失败：{error}"))?;
    Ok(dds)
}

/// 读取配置中显式给出的监听端点。
///
/// `None` 表示未显式配置（Zenoh 的默认值是按模式取值的对象），
/// 或显式配置为空数组。
fn read_configured_endpoints(zenoh: &zenoh::Config) -> Result<Option<Vec<EndPoint>>> {
    let endpoints_json = zenoh
        .get_json("listen/endpoints")
        .map_err(|error| anyhow!("读取 listen.endpoints 失败：{error}"))?;
    let endpoints: Value =
        serde_json::from_str(&endpoints_json).context("listen.endpoints 不是合法 JSON")?;

    match endpoints {
        Value::Array(items) if items.is_empty() => Ok(None),
        Value::Array(items) => {
            let mut endpoints = Vec::with_capacity(items.len());
            for item in items {
                let raw = item
                    .as_str()
                    .ok_or_else(|| anyhow!("listen.endpoints 必须是字符串数组"))?;
                let endpoint = raw.parse::<EndPoint>().map_err(|error| {
                    anyhow!("监听端点 `{raw}` 无效（{error}），需要 `tcp/地址:端口` 形式")
                })?;
                reject_endpoint_retry_overrides(&endpoint)?;
                endpoints.push(endpoint);
            }
            Ok(Some(endpoints))
        }
        // 未显式配置时，Zenoh 的 listen.endpoints 默认是按模式取值的对象。
        Value::Object(_) => Ok(None),
        other => bail!("listen.endpoints 的值类型不受支持：{other}"),
    }
}

const ENDPOINT_RETRY_KEYS: [&str; 4] = [
    "exit_on_failure",
    "retry_period_init_ms",
    "retry_period_max_ms",
    "retry_period_increase_factor",
];

/// 监听端点不能覆盖失败行为：绑定失败必须退出，不能转后台重试。
fn reject_endpoint_retry_overrides(endpoint: &EndPoint) -> Result<()> {
    for key in ENDPOINT_RETRY_KEYS {
        if endpoint.config().get(key).is_some() {
            bail!(
                "监听端点 `{endpoint}` 不能设置 `#{key}`：监听失败必须退出，相关参数由 Buqi 固定"
            );
        }
    }
    Ok(())
}

fn automatic_endpoints(lan_addresses: &[Ipv4Addr]) -> Vec<EndPoint> {
    lan_addresses
        .iter()
        .copied()
        .chain(std::iter::once(Ipv4Addr::LOCALHOST))
        .map(tcp_endpoint)
        .collect()
}

fn tcp_endpoint(address: Ipv4Addr) -> EndPoint {
    format!("tcp/{address}:{DEFAULT_PORT}")
        .parse()
        .expect("tcp 端点格式由本模块构造")
}

fn endpoints_json(endpoints: &[EndPoint]) -> String {
    Value::Array(
        endpoints
            .iter()
            .map(|endpoint| Value::String(endpoint.to_string()))
            .collect(),
    )
    .to_string()
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    fn config(json5: &str) -> zenoh::Config {
        zenoh::Config::from_json5(json5).expect("测试配置无效")
    }

    fn endpoint_strings(endpoints: &[EndPoint]) -> Vec<String> {
        endpoints.iter().map(ToString::to_string).collect()
    }

    #[test]
    fn automatic_endpoints_append_loopback_last() {
        let addresses: Vec<Ipv4Addr> =
            vec!["10.0.0.5".parse().unwrap(), "192.168.1.20".parse().unwrap()];

        assert_eq!(
            endpoint_strings(&automatic_endpoints(&addresses)),
            [
                "tcp/10.0.0.5:7447",
                "tcp/192.168.1.20:7447",
                "tcp/127.0.0.1:7447"
            ]
        );
    }

    #[test]
    fn automatic_without_lan_still_listens_on_loopback() {
        assert_eq!(
            endpoint_strings(&automatic_endpoints(&[])),
            ["tcp/127.0.0.1:7447"]
        );
    }

    #[test]
    fn default_config_has_no_configured_endpoints() {
        assert!(
            read_configured_endpoints(&zenoh::Config::default())
                .expect("读取默认配置失败")
                .is_none()
        );
    }

    #[test]
    fn empty_endpoint_array_means_automatic() {
        let config = config(r#"{ listen: { endpoints: [] } }"#);
        assert!(
            read_configured_endpoints(&config)
                .expect("读取监听端点失败")
                .is_none()
        );
    }

    #[test]
    fn explicit_endpoints_are_used_verbatim() {
        let config =
            config(r#"{ listen: { endpoints: ["tcp/127.0.0.1:8000", "tcp/192.168.1.20:7448"] } }"#);

        assert_eq!(
            endpoint_strings(
                &read_configured_endpoints(&config)
                    .expect("读取监听端点失败")
                    .expect("应显式配置监听端点")
            ),
            ["tcp/127.0.0.1:8000", "tcp/192.168.1.20:7448"]
        );
    }

    #[test]
    fn endpoint_without_transport_prefix_is_rejected() {
        let path =
            std::env::temp_dir().join(format!("buqi-config-invalid-{}.toml", std::process::id()));
        fs::write(&path, "listen = { endpoints = [\"127.0.0.1:7447\"] }\n").unwrap();

        let error = GatewayConfig::load(Some(&path))
            .err()
            .expect("裸地址应被拒绝");
        fs::remove_file(&path).ok();

        assert!(format!("{error:#}").contains("protocol"), "{error:#}");
    }

    #[test]
    fn listen_failure_contract_overrides_global_retry_settings() {
        let config = config(
            r#"{ listen: { endpoints: ["tcp/127.0.0.1:8000"], exit_on_failure: false, timeout_ms: 500 } }"#,
        );
        let gateway = GatewayConfig::from_zenoh(config, || Ok(Vec::new())).expect("配置解析失败");

        let exit_on_failure: Value =
            serde_json::from_str(&gateway.zenoh.get_json("listen/exit_on_failure").unwrap())
                .unwrap();
        assert_eq!(exit_on_failure, Value::Bool(true));
        let timeout_ms: Value =
            serde_json::from_str(&gateway.zenoh.get_json("listen/timeout_ms").unwrap()).unwrap();
        assert_eq!(timeout_ms, Value::from(0));
    }

    #[test]
    fn endpoint_retry_overrides_are_rejected() {
        let config =
            config(r#"{ listen: { endpoints: ["tcp/127.0.0.1:8000#exit_on_failure=false"] } }"#);
        let error = read_configured_endpoints(&config).expect_err("端点级失败重试应被拒绝");
        assert!(
            format!("{error:#}").contains("exit_on_failure"),
            "{error:#}"
        );
    }

    #[test]
    fn endpoint_interface_binding_is_allowed() {
        let config = config(r#"{ listen: { endpoints: ["tcp/127.0.0.1:8000#iface=lo"] } }"#);
        let endpoints = read_configured_endpoints(&config)
            .expect("读取监听端点失败")
            .expect("应显式配置监听端点");
        assert_eq!(endpoints[0].to_string(), "tcp/127.0.0.1:8000#iface=lo");
    }

    #[test]
    fn dds_config_is_taken_from_plugins_and_removed() {
        let mut config = config(r#"{ plugins: { dds: { domain: 199 } } }"#);

        let dds = take_dds_config(&mut config).expect("读取 DDS 配置失败");
        assert_eq!(dds.domain, 199);
        assert!(config.get_json("plugins/dds").is_err());
    }

    #[test]
    fn other_plugins_are_rejected() {
        let mut config = config(r#"{ plugins: { mqtt: {} } }"#);
        let error = take_dds_config(&mut config).expect_err("其他插件应被拒绝");
        assert!(format!("{error:#}").contains("plugins.dds"));
    }

    #[test]
    fn missing_dds_config_uses_plugin_defaults() {
        let mut config = zenoh::Config::default();
        let dds = take_dds_config(&mut config).expect("读取默认 DDS 配置失败");
        assert_eq!(dds.work_thread_num, 2);
    }

    #[test]
    fn contract_is_applied_and_explicit_endpoints_replace_automatic() {
        let config = config(r#"{ listen: { endpoints: ["tcp/127.0.0.1:8000"] } }"#);
        let gateway =
            GatewayConfig::from_zenoh(config, || panic!("显式配置监听端点时不应枚举本机接口"))
                .expect("配置解析失败");

        assert_eq!(gateway.listen_source, ListenSource::Configured);
        let json: Value =
            serde_json::from_str(&gateway.zenoh.get_json("listen/endpoints").unwrap()).unwrap();
        assert_eq!(json, serde_json::json!(["tcp/127.0.0.1:8000"]));
        let mode: Value = serde_json::from_str(&gateway.zenoh.get_json("mode").unwrap()).unwrap();
        assert_eq!(mode, Value::String("router".to_owned()));
        assert_eq!(
            serde_json::from_str::<Value>(
                &gateway
                    .zenoh
                    .get_json("scouting/multicast/enabled")
                    .unwrap()
            )
            .unwrap(),
            Value::Bool(false)
        );
    }

    #[test]
    fn contract_uses_automatic_endpoints_when_not_configured() {
        let gateway = GatewayConfig::from_zenoh(zenoh::Config::default(), || {
            Ok(vec!["192.168.1.20".parse().unwrap()])
        })
        .expect("配置解析失败");

        assert_eq!(
            gateway.listen_source,
            ListenSource::Automatic { lan_addresses: 1 }
        );
        let json: Value =
            serde_json::from_str(&gateway.zenoh.get_json("listen/endpoints").unwrap()).unwrap();
        assert_eq!(
            json,
            serde_json::json!(["tcp/192.168.1.20:7447", "tcp/127.0.0.1:7447"])
        );
    }

    #[test]
    fn interface_enumeration_failure_is_not_treated_as_no_lan() {
        let error = GatewayConfig::from_zenoh(zenoh::Config::default(), || {
            Err(std::io::Error::other("模拟枚举失败"))
        })
        .err()
        .expect("枚举失败应中止启动");
        assert!(format!("{error:#}").contains("枚举本机网络接口失败"));
    }

    #[test]
    fn toml_file_is_loaded_with_native_zenoh_schema() {
        let path =
            std::env::temp_dir().join(format!("buqi-config-explicit-{}.toml", std::process::id()));
        fs::write(
            &path,
            r#"
listen = { endpoints = ["tcp/127.0.0.1:8123"] }

[plugins.dds]
domain = 199
"#,
        )
        .unwrap();

        let gateway = GatewayConfig::load(Some(&path)).expect("加载 TOML 配置失败");
        fs::remove_file(&path).ok();

        assert_eq!(gateway.listen_source, ListenSource::Configured);
        assert_eq!(gateway.dds.domain, 199);
        let json: Value =
            serde_json::from_str(&gateway.zenoh.get_json("listen/endpoints").unwrap()).unwrap();
        assert_eq!(json, serde_json::json!(["tcp/127.0.0.1:8123"]));
    }
}
