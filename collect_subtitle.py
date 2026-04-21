#!/usr/bin/env python3
"""
collect_subtitle.py
imagedic 자막 수집 스크립트
- 단일 영상: VIDEO_ID + YOUTUBE_URL 환경변수로 실행
- 자동 크론: SERVER_URL에서 pending 영상 목록 받아서 일괄 처리
"""
import os
import json
import subprocess
import requests
import tempfile
import re

SERVER_URL      = os.environ.get('SERVER_URL', '')
SUBTITLE_SECRET = os.environ.get('SUBTITLE_SECRET', '')
YOUTUBE_URL     = os.environ.get('YOUTUBE_URL', '')
VIDEO_ID        = os.environ.get('VIDEO_ID', '')
MODE            = os.environ.get('MODE', 'single')  # single | cron


def sec_to_float(time_str):
    """SRT 타임코드 → 초 변환 (00:00:09,000 → 9.0)"""
    time_str = time_str.replace(',', '.')
    parts = time_str.split(':')
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def parse_srt(srt_text):
    """SRT 텍스트 → 클립 리스트"""
    clips = []
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    for block in blocks:
        lines = [l.strip() for l in block.strip().split('\n') if l.strip()]
        if len(lines) < 3:
            continue
        # 타임코드 찾기
        tc_line = None
        for line in lines:
            if '-->' in line:
                tc_line = line
                break
        if not tc_line:
            continue
        m = re.match(r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})', tc_line)
        if not m:
            continue
        start_sec = sec_to_float(m.group(1))
        end_sec   = sec_to_float(m.group(2))
        # 텍스트: 타임코드 이후 줄들
        tc_idx = lines.index(tc_line)
        text_lines = lines[tc_idx+1:]
        # HTML 태그 제거
        text = ' '.join(text_lines)
        text = re.sub(r'<[^>]+>', '', text).strip()
        if not text:
            continue
        clips.append({
            'start_sec': start_sec,
            'end_sec':   end_sec,
            'text_en':   text,
        })
    return clips


def download_subtitle(youtube_url):
    """yt-dlp로 자막 다운로드, SRT 텍스트 반환"""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_template = os.path.join(tmpdir, 'sub')

        # 1차: 공식 영어 자막 시도
        cmd = [
            'yt-dlp',
            '--write-sub',
            '--sub-lang', 'en',
            '--sub-format', 'srt',
            '--skip-download',
            '--no-playlist',
            '-o', output_template,
            youtube_url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # 공식 자막 없으면 자동 자막 시도
        srt_file = None
        for f in os.listdir(tmpdir):
            if f.endswith('.srt') or f.endswith('.en.srt'):
                srt_file = os.path.join(tmpdir, f)
                break

        if not srt_file:
            cmd2 = [
                'yt-dlp',
                '--write-auto-sub',
                '--sub-lang', 'en',
                '--sub-format', 'srt',
                '--skip-download',
                '--no-playlist',
                '-o', output_template,
                youtube_url
            ]
            subprocess.run(cmd2, capture_output=True, text=True)
            for f in os.listdir(tmpdir):
                if f.endswith('.srt') or f.endswith('.en.srt') or 'en' in f:
                    srt_file = os.path.join(tmpdir, f)
                    break

        if not srt_file:
            return None, 'auto-generated 자막도 없음'

        with open(srt_file, 'r', encoding='utf-8', errors='ignore') as fp:
            return fp.read(), None


def send_to_server(video_id, clips, subtitle_lang='en'):
    """서버로 클립 데이터 전송"""
    url = SERVER_URL.rstrip('/') + '/dic/admin/ajax/subtitle_receive.php'
    payload = {
        'secret':       SUBTITLE_SECRET,
        'video_id':     video_id,
        'subtitle_lang': subtitle_lang,
        'clips':        json.dumps(clips, ensure_ascii=False),
    }
    try:
        resp = requests.post(url, data=payload, timeout=120)
        return resp.json()
    except Exception as e:
        return {'result': 'error', 'msg': str(e)}


def report_error(video_id, error_msg):
    """서버에 실패 보고"""
    url = SERVER_URL.rstrip('/') + '/dic/admin/ajax/subtitle_receive.php'
    payload = {
        'secret':   SUBTITLE_SECRET,
        'video_id': video_id,
        'error':    error_msg,
    }
    try:
        requests.post(url, data=payload, timeout=30)
    except:
        pass


def get_pending_videos():
    """서버에서 처리 대기 영상 목록 가져오기"""
    url = SERVER_URL.rstrip('/') + '/dic/admin/ajax/subtitle_receive.php'
    try:
        resp = requests.post(url, data={
            'secret': SUBTITLE_SECRET,
            'action': 'get_pending',
        }, timeout=30)
        data = resp.json()
        return data.get('videos', [])
    except:
        return []


def process_video(video_id, youtube_url):
    print(f'[START] video_id={video_id} url={youtube_url}')

    srt_text, err = download_subtitle(youtube_url)
    if err or not srt_text:
        print(f'[FAIL] 자막 없음: {err}')
        report_error(video_id, err or '자막 없음')
        return False

    clips = parse_srt(srt_text)
    if not clips:
        print('[FAIL] 파싱된 클립 없음')
        report_error(video_id, '파싱된 클립 없음')
        return False

    print(f'[PARSED] {len(clips)}개 클립')

    result = send_to_server(video_id, clips)
    print(f'[SERVER] {result}')

    if result.get('result') == 'ok':
        print(f'[DONE] 클립 {result.get("clips")}개, 단어 {result.get("words")}개 매핑')
        return True
    else:
        print(f'[FAIL] {result.get("msg")}')
        return False


if __name__ == '__main__':
    if not SERVER_URL or not SUBTITLE_SECRET:
        print('[ERROR] SERVER_URL, SUBTITLE_SECRET 환경변수 필요')
        exit(1)

    if MODE == 'single' and VIDEO_ID and YOUTUBE_URL:
        # 단일 영상 처리 (관리자 수동 트리거)
        success = process_video(VIDEO_ID, YOUTUBE_URL)
        exit(0 if success else 1)

    else:
        # 크론: 서버에서 pending 영상 목록 가져와서 일괄 처리
        print('[CRON] pending 영상 목록 조회...')
        videos = get_pending_videos()
        if not videos:
            print('[CRON] 처리할 영상 없음')
            exit(0)

        print(f'[CRON] {len(videos)}개 영상 처리 시작')
        for v in videos:
            process_video(v['video_id'], v['youtube_url'])

        print('[CRON] 완료')