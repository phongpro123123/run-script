from flask import Flask, request, render_template, send_file, redirect, url_for, flash, jsonify
from flask_socketio import SocketIO, emit
import os
import threading
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import pysrt
import requests
from pydub import AudioSegment
from moviepy.editor import VideoFileClip, AudioFileClip, vfx
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'output'
app.secret_key = 'supersecretkey'
socketio = SocketIO(app)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

progress = 0

def emit_log(message):
    socketio.emit('log', message)

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
        global progress
        try:
            progress = 10
            emit_log("Processing audio clips...")
            audio_clips = self.process_audio_clips(self.subtitle_path)
            progress = 50
            emit_log("Processing video clips...")
            video_clips = self.process_video_clips(self.video_path, audio_clips)
            progress = 70
            emit_log("Merging clips...")
            self.merge_clips(video_clips, audio_clips)
            progress = 90
            emit_log("Combining clips...")
            self.combine_clips()
            progress = 100
            emit_log("Video creation successful.")
            if self.callback:
                self.callback("Tạo video thuyết minh thành công.")
        except Exception as e:
            emit_log(f"Error creating video: {e}")
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
                    emit_log(f"Audio {index} already exists: {audio_file}")
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
                            emit_log(f"Audio {index} downloaded: {audio_file}")

                            # Adjust audio speed
                            temp_audio_file = os.path.join(audio_dir, f'temp_audio{index}.mp3')
                            cmd = ['ffmpeg', '-y', '-i', audio_file, '-filter:a', f'atempo={self.atempo}', '-vn', temp_audio_file]
                            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            emit_log(f"Audio {index} speed adjusted: {temp_audio_file}")

                            # Retry mechanism for os.replace
                            for retry in range(max_retries):
                                try:
                                    os.replace(temp_audio_file, audio_file)
                                    emit_log(f"Original audio file overwritten with adjusted speed: {audio_file}")
                                    return audio_file
                                except FileNotFoundError as e:
                                    emit_log(f"Attempt {retry + 1} failed for replacing audio {index}: {e}")
                                    if retry == max_retries - 1:
                                        raise e
                    except Exception as e:
                        emit_log(f"Attempt {attempt + 1} failed for audio {index}: {e}")
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
                emit_log(f"Clip {index} already exists: {clip_file}")
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
                    video_clip.write_videofile(temp_clip, codec='libx264')
                    os.replace(temp_clip, clip_file)
                    emit_log(f"Clip {index} speed adjusted: {clip_file}")
                else:
                    video_clip.write_videofile(clip_file, codec='libx264')
                    emit_log(f"Clip {index} cut: {clip_file}")

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
                    emit_log(f"Error processing clip {futures[future]}: {e}")

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
                    emit_log(f"Clip {index} merged: {merged_file}")
                    # Write the relative path to filelist.txt
                    filelist.write(f"file '{os.path.relpath(merged_file, start=merged_dir)}'\n")

    def combine_clips(self):
        merged_dir = "video_da_ghep"
        filelist_path = os.path.join(merged_dir, "filelist.txt")
        
        # Check if filelist.txt is created correctly
        if not os.path.exists(filelist_path):
            emit_log("Error: filelist.txt was not created.")
            return
        
        # Print the contents of filelist.txt for debugging
        with open(filelist_path, "r") as filelist:
            emit_log("Contents of filelist.txt:")
            emit_log(filelist.read())
        
        # Combine clips using ffmpeg
        cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', filelist_path, '-c:v', 'libx264', '-c:a', 'aac', self.output_path]
        emit_log(f"Running command: {' '.join(cmd)}")  # Print the command for debugging
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Log the output and error from ffmpeg
        emit_log(f"FFmpeg stdout: {result.stdout.decode()}")
        emit_log(f"FFmpeg stderr: {result.stderr.decode()}")
        
        if result.returncode == 0:
            emit_log(f"Final video created: {self.output_path}")
        else:
            emit_log("Error: Failed to create the final video.")
        
        # Clean up
        shutil.rmtree("clip_cut")
        shutil.rmtree("audio_clip")
        shutil.rmtree("video_da_ghep")

@app.route('/progress')
def get_progress():
    global progress
    return jsonify(progress)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_files():
    global progress
    progress = 0
    if 'subtitle' not in request.files or 'video' not in request.files:
        return "No file part", 400

    subtitle = request.files['subtitle']
    video = request.files['video']
    atempo = request.form.get('atempo', 1.25)
    output_filename = request.form.get('output_filename', 'final_video.mp4')

    if subtitle.filename == '' or video.filename == '':
        return "No selected file", 400

    subtitle_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(subtitle.filename))
    video_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(video.filename))
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], secure_filename(output_filename))

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

    subtitle.save(subtitle_path)
    video.save(video_path)

    thread = AudioProcessThread(subtitle_path, video_path, atempo=float(atempo), output_path=output_path)
    thread.start()
    thread.join()

    if not os.path.exists(output_path):
        flash("Error: Final video was not created.")
        return redirect(url_for('index'))

    return redirect(url_for('download_file', filename=output_filename))

@app.route('/download/<filename>')
def download_file(filename):
    file_path = os.path.join(app.config['OUTPUT_FOLDER'], filename)
    if not os.path.exists(file_path):
        flash("Error: File not found.")
        return redirect(url_for('index'))
    return send_file(file_path, as_attachment=True)

if __name__ == "__main__":
    socketio.run(app, debug=True)
