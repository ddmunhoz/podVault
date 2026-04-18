import base64
import sys
import io
import json
import re
import shutil
import subprocess
import time
import music_tag
import mutagen
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import unidecode
import pydantic
from typing import Optional

from datetime import date, datetime
from pathlib import Path
from appdirs import user_config_dir
from dateutil.relativedelta import relativedelta
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import EasyMP3
from PIL import Image
from tqdm import tqdm

from tools.messaging_signal import signalBot
import logging
from logging.handlers import TimedRotatingFileHandler
from tools.appConfig import appConfig
import podcastparser
import urllib.request

import functools

# Register custom ID3 tag for 'year'
EasyID3.RegisterTextKey("year", "TDRC")


class PodVault:
    """
    A class to download, convert, and tag podcast episodes.
    """
    SCRIPT_DIR = Path(__file__).parent
    ROOT_PODCAST_PATH = SCRIPT_DIR / "podcasts"
    CONFIG_DIR = Path(user_config_dir("PodVault"))

    def __init__(self):
        self.logger = self._setup_logging("INFO")
        self.logger.info("Initializing PodVault...")
        self.session = requests.Session()
        # World is a stupid place now, most "humans" block another humans in order to block machines.
        # or just regular greedy
        # below needed to not get blocked by some feeds/servers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        })
        
        # adding retry strategy due to poor RSS podcast feeds I follow
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.config = {}
        self.notify_bot_signal = None
        self._reload_config()  # Initial config load

    def _setup_logging(self,lvl) -> logging.Logger:
        """Sets up a timed rotating file logger."""
        log_path = self.SCRIPT_DIR / "logs" / "log.txt"
        log_path.parent.mkdir(exist_ok=True)
        logger = logging.getLogger("log")
      
        handler = TimedRotatingFileHandler(log_path, when="D", interval=1, backupCount=7)
        logger.setLevel(lvl)
        formatter_logs = logging.Formatter(
            fmt="%(asctime)s %(levelname)-6s %(message)s",
            datefmt="%m-%d-%y %H:%M:%S",
        )
        handler.setFormatter(formatter_logs)
        logger.addHandler(handler)

         # Console handler writing logs to console (stdout)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(lvl)  # Console shows INFO and above
        formatter_console = logging.Formatter(
            fmt="%(asctime)s %(message)s",
            datefmt="%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(formatter_console)
        logger.addHandler(console_handler)

        self.logger_raw = logger
        return logger

    def _reload_config(self):
        """Loads and reloads configuration from the JSON file."""
        config_path = self.SCRIPT_DIR / "config" / "config.json"
        
        try:
            with open(config_path) as f:
                parsedConfig = appConfig.appConfig.load_and_validate(config_path)
                self.config = parsedConfig.get_data()

                # Update logger level dynamically
                log_level = self.config.get("logLevel").upper()
                self.logger_raw.setLevel(log_level)
                
                #self.logger_raw.setLevel(log_level)
                for handler in self.logger_raw.handlers:
                    if isinstance(handler, logging.StreamHandler):
                        handler.setLevel(log_level)
                
                # Update instance attributes from config
                self.check_interval = int(self.config.get("checkInterval"))
                signal_notifier = self.config.get("notifySignal", False) == True
                if signal_notifier:
                    self.notify_bot_signal = signalBot.signalBot(
                        self.config["signalSender"],
                        self.config["signalGroup"],
                        self.config["signalEndpoint"],
                        logger=self.logger
                    )
                else:
                    self.notify_bot_signal = None
            return
        
        except (FileNotFoundError, json.JSONDecodeError, pydantic.ValidationError, ValueError) as e:
            self.logger.critical(f"Could not load or parse config file: {e}")
    
        raise RuntimeError("Failed to load config.")

    def _send_notifications(self, metadata: dict):
        """Sends notifications via configured bots."""
        payload = {
            "📟 ": metadata['show_name'],
            "episode": metadata['name'],
            "duration": metadata['duration'],
            "image_url": metadata['thumb_url']
        }
        self.logger.debug(f"✉️ sending notification {payload}")
        self.notify_bot_signal.sendMessage(payload=payload)

    def _apply_tags(self, file_path: Path, metadata: dict):
        """Applies ID3 tags and artwork to the MP3 file."""
        self.logger.info(f"Applying metadata to '{file_path.name}'...")
        try:
            # Download artwork
            artwork_response = self.session.get(metadata["thumb_url"], timeout=30)
            artwork_response.raise_for_status()
            artwork_data = artwork_response.content

            # Resize artwork to a standard 3000x3000
            image = Image.open(io.BytesIO(artwork_data))
            resized_image = image.resize((3000, 3000), Image.Resampling.LANCZOS)
            
            img_byte_arr = io.BytesIO()
            resized_image.save(img_byte_arr, format="JPEG")
            artwork_final = img_byte_arr.getvalue()

            # Save resized artwork next to the mp3
            artwork_path = file_path.with_suffix(".jpg")
            with open(artwork_path, "wb") as f:
                f.write(artwork_final)

            # Apply tags using music-tag
            tags = music_tag.load_file(file_path)
            tags["artist"] = metadata["show_name"]
            tags["tracktitle"] = metadata["name"]
            tags["comment"] = f"{metadata['description']} - {metadata['release_date']}"
            tags["artwork"] = artwork_final
            tags.save()

            # Apply year tag using mutagen (more reliable for TDRC frame)
            audio = EasyMP3(file_path)
            audio["year"] = metadata["release_year"]
            audio.save()

        except Exception as e:
            self.logger.error(f"Failed to apply tags to {file_path.name}: {e}")

    def _convert_to_mp3(self, temp_path: Path, final_path: Path):
        """Converts the downloaded file to MP3 using ffmpeg."""
        self.logger.info(f"Converting '{temp_path.stem}' to MP3...")
        command = [
            'ffmpeg', '-y', '-i', str(temp_path),
            '-ar', '44100', '-ac', '2', '-b:a', '192k',
            str(final_path)
        ]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace")
        if result.returncode != 0:
            self.logger.error(f"FFmpeg failed for {final_path.name}: {result.stderr}")
        temp_path.unlink() # Clean up temp file

    def _download_stream(self, metadata: dict, temp_path: Path):
        """Downloads the raw audio stream from Spotify."""
        episode_url = metadata['file_url']
        
        with self.session.get(episode_url, stream=True, allow_redirects=True, timeout=(10, 30)) as r:
            if r.status_code != 200:
                r.raise_for_status()  # Will only raise for 4xx codes, so...
                raise RuntimeError(
                    f"Request to {episode_url} returned status code {r.status_code}")
            file_size = int(r.headers.get('Content-Length', 0))

            path = Path(temp_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)

            desc = "(Unknown total file size)" if file_size == 0 else ""
            r.raw.read = functools.partial(
                r.raw.read, decode_content=True)  # Decompress if needed
            with tqdm.wrapattr(r.raw, "read", total=file_size, desc=desc) as r_raw:
                with temp_path.open("wb") as f:
                    shutil.copyfileobj(r_raw, f)

        return temp_path

    def _get_episode_info(self, episode: str, podcast_config: dict) -> dict | None:
        """Fetches and sanitizes episode metadata."""
        try:
            title = episode['title']
            description = episode['description']
            
            published = episode['published']
            published_converted = datetime.fromtimestamp(published).date()
            published_parsed = published_converted.strftime("%Y-%m-%d")
            
            total_time = episode['total_time']

            if episode.get('episode_art_url'):
                episode_art = episode['episode_art_url']
            else:
                episode_art = podcast_config['cover_url']
            
            file_url = episode['enclosures'][0]['url']
            file_size = episode['enclosures'][0]['file_size']
            
            # I'm stupid, overlooked this for years.
            # the obvious double transcode
            # small fix for something that has been alive for a while
            parsed_url = Path(file_url.split('?')[0])
            extension = parsed_url.suffix.lower() or ".mp3"

            return {
                "show_name": self._sanitize_data(""),
                "name": self._sanitize_data(title),
                "description": self._sanitize_data(description),
                "release_year": published_converted.strftime("%Y"),
                "release_date": published_parsed,
                "thumb_url": episode_art,
                "file_url": file_url,
                "file_size": file_size,
                "extension": extension,
                "duration": self._s_to_hms(int(total_time)),
            }
        
        except (requests.RequestException, KeyError, IndexError) as e:
            self.logger.error(f"Failed to get info for episode {episode['title']}: {e}")
            return None

    def download_episode(self, episode: str, podcast_config: dict):
        """Orchestrates the download process for a single episode."""
        
        should_down = True
        if podcast_config.get("filter") == True:
            should_down = self._is_episode_filtered(episode["title"], podcast_config.get("filter_Include"), podcast_config.get("filter_Exclude"))

        if should_down:  
            metadata = self._get_episode_info(episode, podcast_config)
            show_name_override = podcast_config.get("name")
            metadata["show_name"] = show_name_override
            podcast_dir = self.ROOT_PODCAST_PATH / show_name_override
            podcast_dir.mkdir(parents=True, exist_ok=True)
            
            file_name = self._sanitize_filename(metadata['name'])[:100]

            base_filename = f"{metadata['show_name']}-{metadata['release_date']}-{file_name}"
            final_path = podcast_dir / f"{base_filename}.mp3"
            
            # Skip if already downloaded and tagged
            if final_path.is_file() and final_path.stat().st_size > 1000:
                try:
                    if EasyID3(final_path).get("title"):
                        self.logger.info(f"'{final_path.name}' already exists and is tagged. Skipping.")
                        return
                except mutagen.MutagenError:
                    self.logger.warning(f"Corrupt file found, redownloading: {final_path.name}")
                    final_path.unlink()

            self.logger.info(f"Starting download for '{metadata['name']}'")
            
            if metadata['extension'] == ".mp3":
                # continue: download as MP3 instead of double transcode as before
                self._download_stream(metadata, final_path)
            else:
                # Needs conversion
                temp_path = podcast_dir / f"{base_filename}{metadata['extension']}"
                self._download_stream(metadata, temp_path)
                self._convert_to_mp3(temp_path, final_path)
            
            self._apply_tags(final_path, metadata)
            self._send_notifications(metadata)
       
    def _get_show_episodes(self, podcast_config: dict) -> list[str]:
        episodes = []
        
        months_back = int(podcast_config.get("monthsBack"))
        target_date = date.today() - relativedelta(months=months_back)

        self.logger.info(f"--- Checking for new episodes in '{podcast_config['name']}' ---")
        try:
            feedurl = podcast_config["url"]
            response = self.session.get(feedurl, timeout=30)
            response.raise_for_status()
            parsed = podcastparser.parse(feedurl, io.BytesIO(response.content))
            
            podcast_config['cover_url'] = parsed['cover_url']
            if parsed:
                for episode in parsed['episodes']:
                    published = episode['published']
                    published_converted = datetime.fromtimestamp(published).date()
                    if published_converted >= target_date:
                        episodes.append(episode)
                    else:
                        # Episodes are chronological, so we can stop
                        self.logger.info(f"Finished fetching episodes for '{podcast_config['name']}'.")
                        return episodes

        except requests.RequestException as e:
            self.logger.error(f"Error fetching episodes for {podcast_config['name']}: {e}")
        
        return episodes

    def _process_podcast_show(self, podcast_config: dict):
        """Fetches and downloads all episodes for a single configured show."""
        episode_list = self._get_show_episodes(podcast_config)

        if not episode_list:
            self.logger.info(f"No new episodes found for '{podcast_config['name']}'.")
            return
        
        self.logger.info(f"Found {len(episode_list)} episodes to check for '{podcast_config['name']}'.")
        for episode in episode_list:
            self.download_episode(episode, podcast_config)
    
    def run(self):
        """Main execution loop for the downloader."""
        while True:
            start_time = time.time()
            self.logger.info("--- Starting new podcast check cycle ---")
            self._reload_config()  # Check for config changes at the start of each loop
            
            url_file_path = self.config.get('urlFile')
            try:
                with open(url_file_path) as f:
                    url_file_content = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                self.logger.critical(f"Could not load URL file {url_file_path}: {e}")
                raise RuntimeError("Podcast URL file missing!")

            for podcast in url_file_content.get("podcast", []):
                self._process_podcast_show(podcast)

            self.logger.info("--- Cycle finished ---")
            # Sleep for the remainder of the interval
            elapsed_time = time.time() - start_time
            sleep_duration = max(0, self.check_interval - elapsed_time)
            self.logger.info(f"Sleeping for {sleep_duration:.2f} seconds.")
            time.sleep(sleep_duration)

    # --- Static Helper Methods ---
    @staticmethod
    def _s_to_hms(seconds: int) -> str:
        """Converts seconds to H:M:S format."""
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}"

    @staticmethod
    def _sanitize_data(value: str) -> str:
        # Remove ASCII control characters (not printable)
        return ''.join(c for c in value if ord(c) >= 32)
    
    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Sanitizes a string to be a safe filename."""
        filename = unidecode.unidecode(filename)
        filename = filename.replace(" ", "_")
        return re.sub(r"[^A-Za-z0-9_.-]+", "", filename)
    
    @staticmethod
    def _is_episode_filtered(episode_name: str, include_list: list, exclude_list: list) -> bool:
        """Checks if an episode name matches include/exclude filters."""
        name_lower = episode_name.lower()
        included = any(sub in name_lower for sub in include_list) if include_list else True
        excluded = any(sub in name_lower for sub in exclude_list) if exclude_list else False
        return included and not excluded

if __name__ == "__main__":
    podvault = PodVault()
    while True: # Keep trying to run the downloader
        try:
            podvault.run()
        except KeyboardInterrupt:
            print("\nExiting PodVault.")
            break # Exit the loop and the program on manual interrupt
        except Exception:
            # Log the unhandled error
            podvault.logger.exception("A fatal, unhandled error occurred in the main loop. Retrying in 10 minutes.")
            
            # Wait for 10 minutes, some RSS feeds sucks. One day I'll rewrite this
            print("A fatal error occurred. Retrying in 10 minutes...")
            time.sleep(600)
            
            # The 'while True' loop will automatically start the next iteration