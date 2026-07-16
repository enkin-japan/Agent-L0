import AVFoundation
import Foundation

/// 语音输入录音器：AVAudioEngine 采集 → 重采样为 16kHz 单声道 Int16 → WAV。
/// 与 web 端 chat-input 的口径一致（后端 whisper 做 ASR，端上零识别工作）。
final class WavRecorder: ObservableObject {
    @Published var isRecording = false

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var samples: [Int16] = []
    private let lock = NSLock()
    private static let sampleRate: Double = 16000

    /// 请求麦克风权限（平台各自的入口）。
    static func requestPermission() async -> Bool {
        #if os(iOS)
        return await AVAudioApplication.requestRecordPermission()
        #else
        return await AVCaptureDevice.requestAccess(for: .audio)
        #endif
    }

    func start() throws {
        guard !isRecording else { return }
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .default)
        try session.setActive(true)
        #endif
        lock.lock()
        samples.removeAll()
        lock.unlock()

        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0,
              let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                            sampleRate: Self.sampleRate,
                                            channels: 1, interleaved: true),
              let conv = AVAudioConverter(from: inFormat, to: outFormat) else {
            throw NSError(domain: "WavRecorder", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "音频输入不可用"])
        }
        converter = conv

        input.installTap(onBus: 0, bufferSize: 4096, format: inFormat) { [weak self] buffer, _ in
            guard let self, let converter = self.converter else { return }
            let ratio = Self.sampleRate / inFormat.sampleRate
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
            guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
            var fed = false
            var err: NSError?
            converter.convert(to: out, error: &err) { _, outStatus in
                if fed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                fed = true
                outStatus.pointee = .haveData
                return buffer
            }
            guard err == nil, out.frameLength > 0, let channel = out.int16ChannelData else { return }
            let frameCount = Int(out.frameLength)
            self.lock.lock()
            self.samples.append(contentsOf: UnsafeBufferPointer(start: channel[0], count: frameCount))
            self.lock.unlock()
        }

        engine.prepare()
        try engine.start()
        isRecording = true
    }

    /// 停止并产出 WAV 附件；过短（<0.3s）返回 nil。
    func stop() -> Upload? {
        guard isRecording else { return nil }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        converter = nil
        isRecording = false
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif

        lock.lock()
        let pcm = samples
        samples.removeAll()
        lock.unlock()
        guard Double(pcm.count) / Self.sampleRate > 0.3 else { return nil }

        let stamp = Int(Date().timeIntervalSince1970)
        return Upload(filename: "voice-\(stamp).wav", mime: "audio/wav",
                      data: Self.wavData(from: pcm, sampleRate: Int(Self.sampleRate)))
    }

    /// 16-bit 单声道 PCM → WAV（44 字节标准头）。
    static func wavData(from pcm: [Int16], sampleRate: Int) -> Data {
        let byteCount = pcm.count * 2
        var data = Data(capacity: 44 + byteCount)

        func append(_ s: String) { data.append(s.data(using: .ascii)!) }
        func append32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        func append16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }

        append("RIFF")
        append32(UInt32(36 + byteCount))
        append("WAVE")
        append("fmt ")
        append32(16)                                // fmt chunk size
        append16(1)                                 // PCM
        append16(1)                                 // mono
        append32(UInt32(sampleRate))
        append32(UInt32(sampleRate * 2))            // byte rate
        append16(2)                                 // block align
        append16(16)                                // bits per sample
        append("data")
        append32(UInt32(byteCount))
        pcm.withUnsafeBytes { data.append(contentsOf: $0) }
        return data
    }
}
