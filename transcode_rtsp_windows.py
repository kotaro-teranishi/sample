import subprocess
import logging
import time
import os
import threading
import argparse

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("ffmpeg_transcoder_rtsp.log"),
                        logging.StreamHandler()
                    ])

# --- 設定項目 ---
# カメラからの入力RTSP URL
RTSP_INPUT_URL = "rtsp://192.168.201.72:8080/h264.sdp"

# MediaMTXのRTSPプッシュURLのベース (MediaMTXがデフォルトで8554ポートを使用している場合)
MEDIAMTX_RTSP_BASE_URL = "rtsp://localhost:8554/live"

# 出力ストリームの設定リスト
ALL_STREAM_PROFILES = [
    {
        'name': 'high',
        'resolution': '1280x720',
        'video_bitrate': '512k',
        'max_rate_multiplier': 1.3,
        'audio_bitrate': '48k',
        'gop_size': 40,
        'preset': 'superfast',
        'video_codec': 'libx265',
        'audio_codec': 'aac',
        'output_fps': 20,
        'x265_extra_opts': [
            '-tune', 'zerolatency',
            '-x265-params', 'aq-mode=2:aq-strength=0.8:psy-rd=1.5:rdoq=1'
        ]
    },
    {
        'name': 'low',
        'resolution': '1280x720',
        'video_bitrate': '192k',
        'max_rate_multiplier': 2,
        'audio_bitrate': '24k',
        'gop_size': 60,
        'preset': 'superfast',
        'video_codec': 'libx265',
        'audio_codec': 'aac',
        'output_fps': 15,
        'x265_extra_opts': [
            '-tune', 'zerolatency',
            '-x265-params', 'rc-lookahead=10:aq-mode=2:aq-strength=1.0:psy-rd=1.5:rdoq=1'
        ]
    },
    {
        'name': 'quality',
        'resolution': '1280x720',
        'video_bitrate': '256k',
        'max_rate_multiplier': 2.5,
        'audio_bitrate': '32k',
        'gop_size': 100,
        'preset': 'fast',
        'video_codec': 'libx265',
        'audio_codec': 'aac',
        'output_fps': 15,
        'x265_extra_opts': [
            '-x265-params', 'rc-lookahead=30:aq-mode=2:aq-strength=1.0:psy-rd=2.0:rdoq=2:qpmin=10:qpmax=51:nr-intra=8:nr-inter=8'
        ]
    }
]

# --- 設定項目ここまで ---

def build_ffmpeg_command(input_url, profiles_to_use, mediamtx_base_url):
    cmd = [
        'ffmpeg',
        '-i', input_url,
        '-rtsp_transport', 'tcp',
        '-threads', '0',
        '-nostdin',
        '-loglevel', 'info'
    ]

    for i, profile in enumerate(profiles_to_use):
        cmd.extend([
            '-map', '0:v:0',
            '-map', '0:a:0?'
        ])

        if 'x265_extra_opts' in profile:
            cmd.extend(profile['x265_extra_opts'])

        if profile['video_codec'] == 'hevc_nvenc' and 'nvenc_extra_opts' in profile:
            cmd.extend(profile['nvenc_extra_opts'])
        elif profile['video_codec'] == 'hevc_qsv' and 'qsv_extra_opts' in profile:
            cmd.extend(profile['qsv_extra_opts'])

        cmd.extend(['-c:v', profile['video_codec']])

        if 'preset' in profile:
            cmd.extend(['-preset', profile['preset']])

        base_bitrate_k = int(profile['video_bitrate'].replace('k', ''))
        max_rate_k = int(base_bitrate_k * profile['max_rate_multiplier'])
        buf_size_k = max_rate_k * 2

        cmd.extend([
            '-b:v', profile['video_bitrate'],
            '-maxrate', f'{max_rate_k}k',
            '-bufsize', f'{buf_size_k}k',
            '-s', profile['resolution'],
            '-g', str(profile['gop_size']),
            '-keyint_min', str(profile['gop_size']),
            '-sc_threshold', '0',
            '-pix_fmt', 'yuv420p'
        ])

        if 'output_fps' in profile:
            cmd.extend(['-r', str(profile['output_fps'])])

        cmd.extend([
            '-c:a', profile['audio_codec'],
            '-b:a', profile['audio_bitrate'],
            '-ac', '2',
            '-ar', '48000',
            '-af', 'aresample=async=1000:first_pts=0'
        ])

        cmd.extend([
            '-f', 'rtsp',
            f'{mediamtx_base_url}/{profile["name"]}'
        ])

    return cmd

def run_ffmpeg_transcoder(selected_profile_names): # 引数をリストに変更
    """
    FFmpegプロセスを起動し、RTSPストリームを複数のビットレートに変換してMediaMTXにプッシュします。
    """
    profiles_to_run = []
    # ALL_STREAM_PROFILESから選択された名前のプロファイルをフィルタリング
    for name in selected_profile_names:
        found = False
        for p in ALL_STREAM_PROFILES:
            if p['name'] == name:
                profiles_to_run.append(p)
                found = True
                break
        if not found:
            logging.warning(f"指定されたプロファイル '{name}' は見つかりませんでした。スキップします。")

    if not profiles_to_run:
        logging.error(f"実行するプロファイルが一つも指定されませんでした。")
        return

    ffmpeg_cmd = build_ffmpeg_command(RTSP_INPUT_URL, profiles_to_run, MEDIAMTX_RTSP_BASE_URL)

    logging.info("FFmpegトランスコーディングを開始します。")
    logging.info(f"入力RTSPソース: {RTSP_INPUT_URL}")
    logging.info(f"MediaMTXプッシュ先ベースURL: {MEDIAMTX_RTSP_BASE_URL}")

    for profile in profiles_to_run:
        logging.info(f"  --> 出力ストリーム '{profile['name']}': {MEDIAMTX_RTSP_BASE_URL}/{profile['name']}")
        logging.info(f"    解像度: {profile['resolution']}, Vビットレート: {profile['video_bitrate']}, Aビットレート: {profile['audio_bitrate']}")
        if 'output_fps' in profile:
            logging.info(f"    フレームレート: {profile['output_fps']}fps")
        if 'x265_extra_opts' in profile:
            logging.info(f"    x265追加オプション: {' '.join(profile['x265_extra_opts'])}")

    logging.info(f"\n実行されるFFmpegコマンド:\n{' '.join(ffmpeg_cmd)}\n")

    process = None
    try:
        process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

        def log_stream_output(pipe, prefix):
            for line in pipe:
                if "frame=" in line or "speed=" in line:
                    logging.info(f"[{prefix}] {line.strip()}")
                elif "error" in line.lower() or "failed" in line.lower() or "no such file or directory" in line.lower() or "invalid argument" in line.lower():
                    logging.warning(f"[{prefix}] {line.strip()}")
                else:
                    logging.debug(f"[{prefix}] {line.strip()}")

        stdout_thread = threading.Thread(target=log_stream_output, args=(process.stdout, "FFMPEG_STDOUT"))
        stderr_thread = threading.Thread(target=log_stream_output, args=(process.stderr, "FFMPEG_STDERR"))
        stdout_thread.daemon = True
        stderr_thread.daemon = True
        stdout_thread.start()
        stderr_thread.start()

        logging.info("FFmpegプロセスが実行中です。Ctrl+C で停止します。")
        process.wait()

        logging.info(f"FFmpegプロセス終了。終了コード: {process.returncode}")

    except KeyboardInterrupt:
        logging.info("Ctrl+C が検出されました。FFmpegプロセスを終了します。")
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logging.warning("FFmpegプロセスがGraceful shutdownに応答しませんでした。強制終了します。")
                process.kill()
                process.wait()
        logging.info("FFmpegプロセスが終了しました。")
    except Exception as e:
        logging.error(f"FFmpeg実行中に予期せぬエラーが発生しました: {e}")
        if process and process.poll() is None:
            process.kill()
    finally:
        if stdout_thread.is_alive():
            stdout_thread.join(timeout=1)
        if stderr_thread.is_alive():
            stderr_thread.join(timeout=1)


if __name__ == "__main__":
    logging.info("------------------------------------------------------------")
    logging.info("このスクリプトを実行する前に、以下を確認してください:")
    logging.info("1. **MediaMTXが起動していること。** (例: `mediamtx.exe`)")
    logging.info("2. **FFmpegがシステムにインストールされており、'ffmpeg.exe' がPath環境変数に追加されていること。**")
    logging.info("   (または、FFmpegの実行ファイルのフルパスをスクリプト内で指定してください)")
    logging.info("3. **入力RTSPソースが利用可能であること。** (例: 'rtsp://192.168.201.72:8080/h264.sdp' がライブで利用可能か)")
    logging.info("4. **ハードウェアアクセラレーションを使用する場合、FFmpegビルドが対応していること。**")
    logging.info("------------------------------------------------------------")

    parser = argparse.ArgumentParser(description="FFmpeg RTSPトランスコーダー。出力プロファイルを選択します。")
    parser.add_argument(
        '--profile',
        type=str,
        nargs='+', # ★★★ ここを 'nargs='+' に変更 ★★★
        choices=[p['name'] for p in ALL_STREAM_PROFILES], # 利用可能なプロファイル名を動的に取得
        default=['high', 'low'], # デフォルトをリストで指定
        help="実行する出力プロファイルのリスト: 'high', 'low', 'quality' から複数選択可能 (例: --profile quality low)。デフォルトは 'high' と 'low' です。"
    )
    args = parser.parse_args()

    # run_ffmpeg_transcoder 関数に、選択されたプロファイル名のリストを直接渡す
    run_ffmpeg_transcoder(args.profile)