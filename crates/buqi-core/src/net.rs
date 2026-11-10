//! Linux 网络接口快照与局域网监听地址选择。
//!
//! 首版只支持 Linux + IPv4：自动监听普通物理接口上的 RFC1918 私有地址。
//! VPN/TUN、容器网桥、veth、bridge、VLAN 等不自动选择，需要时在配置文件中
//! 显式列出监听端点。

use std::collections::BTreeMap;
use std::net::Ipv4Addr;

use nix::ifaddrs::getifaddrs;
use nix::net::if_::InterfaceFlags;

/// 一张网络接口在启动时刻的快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Interface {
    pub name: String,
    /// `IFF_UP` 且 `IFF_RUNNING`：已启用，并有载波或已完成关联。
    pub active: bool,
    pub loopback: bool,
    /// 接口由真实设备承载（sysfs 中存在 `device` 链接）。
    pub physical: bool,
    pub ipv4: Vec<Ipv4Addr>,
}

/// 读取当前接口快照。
///
/// 返回 `Err` 表示 `getifaddrs` 失败；调用方必须把它当作启动失败，而不是
/// “本机没有局域网地址”。`getifaddrs` 的错误必须在这里保留——上游封装
/// （如 `pnet_datalink`）会把失败吞成空列表。
pub fn snapshot() -> std::io::Result<Vec<Interface>> {
    let addresses = getifaddrs().map_err(std::io::Error::from)?;
    let mut interfaces: BTreeMap<String, Interface> = BTreeMap::new();

    for address in addresses {
        let interface = interfaces
            .entry(address.interface_name.clone())
            .or_insert_with(|| Interface {
                name: address.interface_name.clone(),
                active: false,
                loopback: false,
                physical: is_physical(&address.interface_name),
                ipv4: Vec::new(),
            });

        interface.active = address.flags.contains(InterfaceFlags::IFF_UP)
            && address.flags.contains(InterfaceFlags::IFF_RUNNING);
        interface.loopback = address.flags.contains(InterfaceFlags::IFF_LOOPBACK);
        if let Some(inet) = address
            .address
            .as_ref()
            .and_then(|address| address.as_sockaddr_in())
        {
            interface.ipv4.push(inet.ip());
        }
    }

    Ok(interfaces.into_values().collect())
}

/// 物理网卡在 sysfs 下带有 `device` 链接；veth、bridge、tun、VLAN 等没有。
fn is_physical(name: &str) -> bool {
    std::path::Path::new("/sys/class/net")
        .join(name)
        .join("device")
        .exists()
}

/// 从快照中选择自动监听的局域网 IPv4 地址。
///
/// 条件是：物理接口、非回环、已启用并在运行、地址属于 RFC1918 私有范围。
/// 结果按数值排序并去重；没有匹配时返回空列表。
pub fn lan_addresses(interfaces: &[Interface]) -> Vec<Ipv4Addr> {
    let mut addresses: Vec<Ipv4Addr> = interfaces
        .iter()
        .filter(|interface| interface.active && !interface.loopback && interface.physical)
        .flat_map(|interface| interface.ipv4.iter().copied())
        .filter(|address| address.is_private())
        .collect();
    addresses.sort_unstable();
    addresses.dedup();
    addresses
}

#[cfg(test)]
mod tests {
    use super::*;

    fn interface(name: &str, physical: bool, ipv4: &[&str]) -> Interface {
        Interface {
            name: name.to_owned(),
            active: true,
            loopback: false,
            physical,
            ipv4: ipv4.iter().map(|ip| ip.parse().unwrap()).collect(),
        }
    }

    #[test]
    fn selects_private_ipv4_on_active_physical_interfaces() {
        let interfaces = vec![
            interface("enp1s0", true, &["192.168.1.20"]),
            interface("wlp2s0", true, &["10.0.0.5"]),
        ];

        assert_eq!(
            lan_addresses(&interfaces),
            vec![Ipv4Addr::new(10, 0, 0, 5), Ipv4Addr::new(192, 168, 1, 20)]
        );
    }

    #[test]
    fn skips_non_private_inactive_virtual_and_loopback_interfaces() {
        let mut inactive = interface("enp3s0", true, &["192.168.2.10"]);
        inactive.active = false;
        let mut loopback = interface("lo", false, &["127.0.0.1"]);
        loopback.loopback = true;

        let interfaces = vec![
            interface("enp1s0", true, &["8.8.8.8", "169.254.3.4"]),
            interface("docker0", false, &["172.17.0.1"]),
            interface("veth1234", false, &["10.1.2.3"]),
            inactive,
            loopback,
        ];

        assert!(lan_addresses(&interfaces).is_empty());
    }

    #[test]
    fn sorts_and_deduplicates_addresses_across_interfaces() {
        let interfaces = vec![
            interface("enp2s0", true, &["192.168.1.30"]),
            interface("enp1s0", true, &["192.168.1.20", "192.168.1.20"]),
        ];

        assert_eq!(
            lan_addresses(&interfaces),
            vec![
                Ipv4Addr::new(192, 168, 1, 20),
                Ipv4Addr::new(192, 168, 1, 30)
            ]
        );
    }

    #[test]
    fn snapshot_reports_actual_interfaces_and_errors_are_fallible() {
        let interfaces = snapshot().expect("读取本机接口快照失败");
        assert!(
            lan_addresses(&interfaces).iter().all(Ipv4Addr::is_private),
            "自动选择的地址必须全部是私有 IPv4"
        );
    }
}
