# import subprocess
#
# def download_audio(url):
#     command = [
#         "yt-dlp",
#         "-f", "bestaudio",
#         "--extract-audio",
#         "--audio-format", "mp3",
#         url
#     ]
#
#     try:
#         subprocess.run(command, check=True)
#         print("Download complete!")
#     except subprocess.CalledProcessError as e:
#         print("Error:", e)
#
# # Example usage
# download_audio("https://www.youtube.com/watch?v=fi2tSZT4zDo")