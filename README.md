# Media Diversity Sampler

**A small library that still contains one of everything.**

Testing a media server, a player or a transcoder on 20 TB of video is slow. Testing on a handful of random files misses the odd ones: the HDR10+ file, the interlaced one, the one with a strange codec.
This tool scans your library with FFprobe, picks a **small, varied sample** that covers as many different kinds of video as possible, and copies it to a test library of its own.

## What it does

* Scans one or more media folders with FFprobe and remembers the results, so the next scan only looks at new and changed files.
* Sorts every video into SDR, HDR or **uncertain** (when the file does not say clearly), and selects a sample from them: all videos, only SDR, only HDR or only the uncertain ones.
* Shows how many files you need to cover everything (the *recommended minimum*) before it asks how many you want. The selection is the same every time for the same library.
* Shows which files were picked and why, and lets you **preview** before anything is copied.
* **Copies the sample** to one or more output folders. A build can be stopped with Ctrl+C and resumed later, and files that are already copied are not copied again.
* **Never invents values.** When FFprobe does not report something, the file is shown as unknown or unspecified, never guessed.
* **Understands network locations.** On Windows, UNC paths, mapped drives and shortcuts (`.lnk`) that lead to the same share are recognized as one place. On Linux, local disks, CIFS and NFS mounts, bind mounts and automounts are recognized.
* English and Slovenian interface; more languages can be added.

Your original videos are only read, never changed.

## What you need

| | |
|---|---|
| Python | 3.10 or newer |
| FFprobe | Windows: `ffmpeg\bin\ffprobe.exe` next to `main.py`. Linux: `ffmpeg/bin/ffprobe` next to `main.py`, otherwise the system one |

The program does not download FFmpeg and creates no `ffmpeg` folder for you.

## Install on Windows

1. Install Python from <https://www.python.org/downloads/>. Tick **Add python.exe to PATH** in the installer.
2. Download this project (green **Code** button, **Download ZIP**) and unpack it, for example to `C:\MediaDiversitySampler`.
3. Download an FFmpeg build from <https://www.gyan.dev/ffmpeg/builds/> and unpack it so that you get:
   ```
   MediaDiversitySampler\
       main.py
       ffmpeg\
           bin\
               ffprobe.exe
   ```
   On Windows FFprobe is only taken from this folder, never from `PATH`.
4. Start it:
   ```
   py -3 main.py
   ```

## Install on Linux

1. Install Python and FFmpeg. On Debian and Ubuntu: `sudo apt install python3 ffmpeg`. On Fedora: `sudo dnf install python3 ffmpeg-free`.
2. Get the project:
   ```
   git clone https://github.com/GlacialPigeon/MediaDiversitySampler.git
   cd MediaDiversitySampler
   ```
3. Start it:
   ```
   python3 main.py
   ```

No other Python package is needed.

**Your own FFmpeg build (optional).** The system FFmpeg is enough. If you want a specific build instead, download a static one into the `ffmpeg/bin` folder next to `main.py`; `ffprobe` there is used before the system one. The same commands work on Debian, Ubuntu and Fedora (use `arm64` instead of `amd64` on a 64-bit ARM machine such as a Raspberry Pi):
```
sudo apt install curl xz-utils        # Debian, Ubuntu
sudo dnf install curl xz              # Fedora
cd MediaDiversitySampler
mkdir -p ffmpeg/bin
curl -L -o ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xf ffmpeg.tar.xz --strip-components=1 -C ffmpeg/bin --wildcards '*/ffmpeg' '*/ffprobe'
rm ffmpeg.tar.xz
ffmpeg/bin/ffprobe -version
```

## First run

1. **Configuration > Media Sources > Add Source**: add the folders that hold your videos. You can also import a list from a text file.
2. **Configuration > Outputs > Add Output**: add the folder where the test library should be built.
3. **Scan Media**: the first scan reads every file; later scans are quick.
4. Pick **Selection - ALL** (or SDR, HDR, Uncertain). It shows the recommended minimum and asks how many files you want.
5. **Preview** shows what would be copied. **Build / Resume** copies it.

Press Ctrl+C once to stop gracefully; start **Build / Resume** later and it continues where it stopped.

## More

Details (platforms, storage identity, localization, project layout, saved files) are in the [technical reference](technical.md).

### More tools

Three separate tools that work well together:

* [Media Cataloger](https://github.com/GlacialPigeon/MediaCataloger): catalogs your library with FFprobe and writes Excel, JSON and TXT reports.
* Media Diversity Sampler: this tool.
* [Media Trickplay Generator](https://github.com/GlacialPigeon/MediaTrickplayGenerator): makes the seek-bar preview pictures for Jellyfin on your stronger PC.

## License

See [LICENSE](LICENSE).
