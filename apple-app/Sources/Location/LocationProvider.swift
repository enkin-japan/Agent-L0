import CoreLocation
import Foundation

/// 一次性定位提供者（get_location 工具回程的数据源）。
/// 后端在会话事件流上发 location_request，附着中的客户端用本类取一次定位回 POST——
/// 定位源永远是"用户正在用的这台设备"（Mac 前=Wi-Fi 定位，iPhone 在外=GPS）。
/// 首次调用触发系统授权弹窗（TCC 一次性许诺）；坐标上报前截到 3 位小数（约百米），
/// 精确轨迹不出设备。与 WavRecorder 同模式：权限归 app，数据走既有通道。
@MainActor
final class LocationProvider: NSObject, CLLocationManagerDelegate {
    static let shared = LocationProvider()

    struct Fix {
        let latitude: Double
        let longitude: Double
        let accuracyM: Double
        let place: String?
    }

    enum Failure: Error {
        case denied          // 用户拒绝/系统限制
        case noFix(String)   // 授权了但拿不到位置（超时/硬件/服务关闭）
    }

    private lazy var manager: CLLocationManager = {
        let m = CLLocationManager()
        m.delegate = self
        m.desiredAccuracy = kCLLocationAccuracyHundredMeters
        return m
    }()
    private var authContinuation: CheckedContinuation<Void, Never>?
    private var fixContinuation: CheckedContinuation<CLLocation, Error>?
    /// 定位回调超时代（防 CoreLocation 永不回调时 continuation 悬挂泄漏）
    private var fixGeneration = 0

    func currentFix() async throws -> Fix {
        if manager.authorizationStatus == .notDetermined {
            // 首次：弹系统授权窗，等 didChangeAuthorization 回调（用户点掉弹窗）
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                authContinuation = cont
                manager.requestWhenInUseAuthorization()
            }
        }
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            break
        default:
            throw Failure.denied
        }

        fixGeneration += 1
        let gen = fixGeneration
        let location: CLLocation = try await withCheckedThrowingContinuation { cont in
            fixContinuation = cont
            manager.requestLocation()
            // 15s 兜底：backend 通道 25s 超时，客户端先一步以明确错误应答
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if self.fixGeneration == gen, let pending = self.fixContinuation {
                    self.fixContinuation = nil
                    pending.resume(throwing: Failure.noFix("定位超时"))
                }
            }
        }

        // 反地理编码尽力而为：拿不到地名就只报坐标（不因此失败）
        var place: String? = nil
        if let mark = try? await CLGeocoder().reverseGeocodeLocation(location).first {
            let parts = [mark.administrativeArea, mark.locality, mark.subLocality]
                .compactMap { $0 }
            if !parts.isEmpty { place = parts.joined() }
        }

        func coarse(_ v: CLLocationDegrees) -> Double { (v * 1000).rounded() / 1000 }
        return Fix(latitude: coarse(location.coordinate.latitude),
                   longitude: coarse(location.coordinate.longitude),
                   accuracyM: max(location.horizontalAccuracy, 0),
                   place: place)
    }

    // MARK: - CLLocationManagerDelegate（manager 建在主线程，回调落主线程）

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            // CoreLocation 在 delegate 刚挂上时会立刻回调一次（此刻弹窗才出、状态仍
            // .notDetermined）——这次不算数，否则会拿着未决状态判"拒绝"提前应答
            //（实测 57：首查弹窗与"定位不可用"同屏）。只有用户点掉弹窗后的
            // 决定性回调才唤醒等待。
            guard status != .notDetermined else { return }
            self.authContinuation?.resume()
            self.authContinuation = nil
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager,
                                     didUpdateLocations locations: [CLLocation]) {
        let last = locations.last
        Task { @MainActor in
            guard let cont = self.fixContinuation else { return }
            self.fixContinuation = nil
            self.fixGeneration += 1
            if let last {
                cont.resume(returning: last)
            } else {
                cont.resume(throwing: Failure.noFix("空定位结果"))
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager,
                                     didFailWithError error: Error) {
        let msg = error.localizedDescription
        Task { @MainActor in
            guard let cont = self.fixContinuation else { return }
            self.fixContinuation = nil
            self.fixGeneration += 1
            cont.resume(throwing: Failure.noFix(msg))
        }
    }
}
