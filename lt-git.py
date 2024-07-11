import os
import shutil
import threading
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import pysrt
import requests
from pydub import AudioSegment
from moviepy.editor import VideoFileClip, vfx

class AudioProcessThread(threading.Thread):
    def __init__(self, subtitle_path, video_path, atempo=1.25, voice="hn_female_ngochuyen_fast_news_48k-thg", output_path="final_video.mp4", callback=None):
        super().__init__()
        self.subtitle_path = subtitle_path
        self.video_path = video_path
        self.atempo = atempo
        self.voice = voice
        self.output_path = output_path
        self.callback = callback

    def run(self):
        try:
            # Process audio clips first
            audio_clips = self.process_audio_clips(self.subtitle_path)
            # Process video clips
            video_clips = self.process_video_clips(self.video_path, audio_clips)
            # Merge audio and video clips
            self.merge_clips(video_clips, audio_clips)
            # Combine all merged clips into the final video
            self.combine_clips()
            if self.callback:
                self.callback("Tạo video thuyết minh thành công.")
        except Exception as e:
            if self.callback:
                self.callback(f"Lỗi khi tạo video: {e}")

    def process_audio_clips(self, subtitle_path):
        subtitles = pysrt.open(subtitle_path)
        audio_dir = "audio_clip"
        os.makedirs(audio_dir, exist_ok=True)
        audio_clips = [None] * len(subtitles)  # Prepare a list to store paths to audio files

        def download_and_adjust_audio(sub, index):
            if sub.text.strip():
                audio_file = os.path.join(audio_dir, f'audio{index}.mp3')
                if os.path.exists(audio_file):
                    print(f"Audio {index} already exists: {audio_file}")
                    return audio_file

                url = "https://mobifone.ai/api/v1/convert-tts"
                payload = {
                    'input_text': sub.text,
                    'voice': self.voice,
                    'bit_rate': '64000'
                }
                max_retries = 5
                for attempt in range(max_retries):
                    try:
                        response = requests.get(url, params=payload)
                        data = response.json()
                        if 'download' in data:
                            mp3_url = data['download']
                            urllib.request.urlretrieve(mp3_url, audio_file)
                            print(f"Audio {index} downloaded: {audio_file}")

                            # Adjust audio speed
                            temp_audio_file = os.path.join(audio_dir, f'temp_audio{index}.mp3')
                            cmd = ['ffmpeg', '-y', '-i', audio_file, '-filter:a', f'atempo={self.atempo}', '-vn', temp_audio_file]
                            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            print(f"Audio {index} speed adjusted: {temp_audio_file}")

                            # Retry mechanism for os.replace
                            for retry in range(max_retries):
                                try:
                                    os.replace(temp_audio_file, audio_file)
                                    print(f"Original audio file overwritten with adjusted speed: {audio_file}")
                                    return audio_file
                                except FileNotFoundError as e:
                                    print(f"Attempt {retry + 1} failed for replacing audio {index}: {e}")
                                    if retry == max_retries - 1:
                                        raise e
                    except Exception as e:
                        print(f"Attempt {attempt + 1} failed for audio {index}: {e}")
                        if attempt == max_retries - 1:
                            raise e
                return None

        # Use ThreadPoolExecutor to download and adjust audio files in parallel
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(download_and_adjust_audio, sub, index): index for index, sub in enumerate(subtitles)}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    audio_clips[futures[future]] = result  # Store the path to the audio file in the corresponding index

            return audio_clips

    def process_video_clips(self, video_path, audio_clips):
        subtitles = pysrt.open(self.subtitle_path)
        clip_dir = "clip_cut"
        os.makedirs(clip_dir, exist_ok=True)
        video_clips = [None] * len(subtitles)  # Prepare a list to store paths to video files

        def process_clip(index, sub):
            start_time = (sub.start.hours * 3600 + sub.start.minutes * 60 + sub.start.seconds) + sub.start.milliseconds / 1000.0
            end_time = (sub.end.hours * 3600 + sub.end.minutes * 60 + sub.end.seconds) + sub.end.milliseconds / 1000.0
            duration = end_time - start_time

            clip_file = os.path.join(clip_dir, f'clip{index}.mp4')
            if os.path.exists(clip_file):
                print(f"Clip {index} already exists: {clip_file}")
                return clip_file

            video_clip = VideoFileClip(video_path).subclip(start_time, end_time)

            try:
                # Calculate the speed for the current clip
                audio_duration = AudioSegment.from_mp3(audio_clips[index]).duration_seconds if audio_clips[index] else duration
                speed = duration / audio_duration

                # Adjust the speed of the clip using moviepy
                if speed != 1:  # Only adjust if the speed is different from 1
                    temp_clip = os.path.join(clip_dir, f'temp_clip{index}.mp4')
                    video_clip = video_clip.fx(vfx.speedx, speed)
                    video_clip.write_videofile(temp_clip, codec='libx264', ffmpeg_params=['-c:v', 'h264_nvenc'])
                    os.replace(temp_clip, clip_file)
                    print(f"Clip {index} speed adjusted: {clip_file}")
                else:
                    video_clip.write_videofile(clip_file, codec='libx264', ffmpeg_params=['-c:v', 'h264_nvenc'])
                    print(f"Clip {index} cut: {clip_file}")

                return clip_file
            finally:
                video_clip.close()  # Ensure the clip is closed to release resources

        # Use ThreadPoolExecutor to process video clips in parallel
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_clip, index, sub): index for index, sub in enumerate(subtitles)}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        video_clips[futures[future]] = result  # Store the path to the video file in the corresponding index
                except Exception as e:
                    print(f"Error processing clip {futures[future]}: {e}")

        return video_clips
    
    def merge_clips(self, video_clips, audio_clips):
        merged_dir = "video_da_ghep"
        os.makedirs(merged_dir, exist_ok=True)
        filelist_path = os.path.join(merged_dir, "filelist.txt")

        with open(filelist_path, "w") as filelist:
            for index, (video_clip, audio_clip) in enumerate(zip(video_clips, audio_clips)):
                if video_clip and audio_clip:
                    merged_file = os.path.join(merged_dir, f'merged{index}.mp4')
                    cmd = ['ffmpeg', '-y', '-i', video_clip, '-i', audio_clip, '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v:0', '-map', '1:a:0', '-shortest', merged_file]
                    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    print(f"Clip {index} merged: {merged_file}")
                    # Write the relative path to filelist.txt
                    filelist.write(f"file '{os.path.relpath(merged_file, start=merged_dir)}'\n")

    def combine_clips(self):
        merged_dir = "video_da_ghep"
        filelist_path = os.path.join(merged_dir, "filelist.txt")
        
        # Check if filelist.txt is created correctly
        if not os.path.exists(filelist_path):
            print("Error: filelist.txt was not created.")
            return
        
        # Print the contents of filelist.txt for debugging
        with open(filelist_path, "r") as filelist:
            print("Contents of filelist.txt:")
            print(filelist.read())
        
        # Combine clips using ffmpeg
        cmd = ['ffmpeg', '-y', '-hwaccel', 'dxva2', '-f', 'concat', '-safe', '0', '-i', filelist_path, '-c', 'copy', self.output_path]
        print(f"Running command: {' '.join(cmd)}")  # Print the command for debugging
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Log the output and error from ffmpeg
        print(f"FFmpeg stdout: {result.stdout.decode()}")
        print(f"FFmpeg stderr: {result.stderr.decode()}")
        
        if result.returncode == 0:
            print(f"Final video created: {self.output_path}")
        else:
            print("Error: Failed to create the final video.")
        
        # Clean up
        shutil.rmtree("clip_cut")
        shutil.rmtree("audio_clip")
        shutil.rmtree("video_da_ghep")
        
if __name__ == "__main__":
    # Directly define the paths and settings here
    subtitle_path = "srt.srt"
    video_path = "video.mp4"
    atempo = 1.20
    output_path = "final_video.mp4"

    print("Bắt đầu quá trình tạo video thuyết minh...")
    thread = AudioProcessThread(subtitle_path, video_path, atempo=atempo, output_path=output_path)
    thread.start()
    thread.join()  # Wait for the thread to finish
    print("Quá trình tạo video thuyết minh đã hoàn thành.")
