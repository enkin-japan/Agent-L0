from pathlib import Path
import io
import uuid
import shutil
import mimetypes
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import UploadFile

# 注：torch / librosa / soundfile / silero_vad 刻意**不在顶层 import**——它们只被
# separate_audio（原生音频直喂路）用到，而那条路由 inference.modalities 含 "audio"
# 才启用（默认不含 → 音频一律 ASR 转写）。顶层 import 会让 torch(385M) 无条件进内存，
# 且让 edge 打包必须背上整条音频依赖（R5-2/D.14：整条音频路已移设备 A）。


class FileHandler:
    """
    基础设施服务：负责处理用户上传的多模态文件（图片、音频）。
    包括音频的 VAD 智能切片、文件的本地化落盘等。

    vad_model 为 None = 原生音频直喂未启用（modalities 不含 audio）；此时
    separate_audio 不该被调用（Ingestion 的 native_audio 门控负责），调用即明确报错。
    """
    def __init__(self, vad_model: Any, audio_max_length: int = 29):
        self.vad_model = vad_model
        self.audio_max_length = audio_max_length

    async def separate_audio(self, audio: UploadFile, name: str, audio_dir: Path) -> List[Path]:
        """
        基于 Silero VAD 的过大音频智能拆解算法

        重依赖在此惰性载入（见模块头注）：本方法只在原生音频直喂启用时被调到。
        """
        if self.vad_model is None:
            raise RuntimeError(
                "原生音频直喂未启用（inference.modalities 不含 'audio'），VAD 未加载。"
                "音频应走 ASR 转写路——这里被调到说明 Ingestion 的 native_audio 门控漏了。")
        import torch
        import librosa
        import soundfile
        from silero_vad import get_speech_timestamps

        audio_path = []
        audio_byte = await audio.read()
        virtual_file = io.BytesIO(audio_byte)
        
        # 统一重采样到 16000Hz 供 VAD 及后续模型使用
        audio_data, sample_rate = librosa.load(virtual_file, sr=16000)
        duration = len(audio_data) / sample_rate
        wav = torch.from_numpy(audio_data)
        
        # 获取语音活动时间戳
        speech_timestamps = get_speech_timestamps(
            wav, 
            self.vad_model,
            return_seconds=True
        )

        cut_points = []
        cut_point = 0
        for i in speech_timestamps:
            start = i["start"]
            end = i["end"]
            while start - cut_point > self.audio_max_length:
                cut_point += self.audio_max_length
                cut_points.append(cut_point)
            if end - cut_point > self.audio_max_length:
                if start - 0.2 > cut_point:
                    cut_point = start - 0.2
                    cut_points.append(cut_point)
                while end - cut_point > self.audio_max_length:
                    cut_point += self.audio_max_length
                    cut_points.append(cut_point)
        
        while duration - cut_point > self.audio_max_length:
            cut_point += self.audio_max_length
            cut_points.append(cut_point)
        
        # 根据切点落盘
        if cut_points:
            cut_count = len(cut_points)
            for i in range(cut_count):
                if i == 0:
                    audio_chunk = audio_data[:int(cut_points[i] * sample_rate)]
                else:
                    audio_chunk = audio_data[int(cut_points[i-1] * sample_rate):int(cut_points[i] * sample_rate)]
                audio_chunk_path = audio_dir / f"{name}_{i}.wav"
                audio_path.append(audio_chunk_path)
                soundfile.write(audio_chunk_path, audio_chunk, sample_rate)
            
            last_chunk = audio_data[int(cut_points[-1] * sample_rate):]
            if any(last_chunk):
                last_chunk_path = audio_dir / f"{name}_{cut_count}.wav"
                audio_path.append(last_chunk_path)
                soundfile.write(last_chunk_path, last_chunk, sample_rate)
        else:
            audio_chunk_path = audio_dir / f"{name}.wav"
            audio_path.append(audio_chunk_path)
            soundfile.write(audio_chunk_path, audio_data, sample_rate)
            
        return audio_path

    async def analysis_files(self, files: Optional[List[UploadFile]], image_dir: Path, audio_dir: Path) -> Dict[str, List[str]]:
        """
        统一解析前端传入的混合文件，分类存储为物理路径
        """
        result = {
            "image": [],
            "audio": []
        }
        if not files:
            return result
            
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        for file in files:
            if file and file.filename:
                file_ext = Path(file.filename).suffix
                file_name = f"{timestamp}_{uuid.uuid4().hex}"
                mimetype, _ = mimetypes.guess_type(file.filename)
                
                if mimetype and mimetype.startswith("image/"):
                    file_path = image_dir / f"{file_name}{file_ext}"
                    try:
                        with open(file_path, "wb") as buffer:
                            shutil.copyfileobj(file.file, buffer)
                        # 统一转为 str：路径会进入 history.jsonl 与 API 响应体（JSON 序列化）
                        result["image"].append(str(file_path))
                    finally:
                        file.file.close()
                        
                elif mimetype and mimetype.startswith("audio/"):
                    try:
                        # 触发 VAD 智能切片逻辑
                        file_paths = await self.separate_audio(file, file_name, audio_dir)
                        # 统一转为 str：路径会进入 history.jsonl 与 API 响应体（JSON 序列化）
                        result["audio"].extend(str(p) for p in file_paths)
                    finally:
                        file.file.close()
        return result