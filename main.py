import re
import requests
import time
import m3u8
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from typing import Optional, Dict, Any
import os
from urllib.parse import urlparse, urljoin
import threading
import logging
import curl_cffi
import atexit

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ViewerCounter:
    def __init__(self):
        self._count = 0
        self._lock = threading.Lock()
        
    def increment(self):
        with self._lock:
            self._count += 1
            logger.info(f"Active viewers: {self._count}")
            
    def decrement(self):
        with self._lock:
            self._count = max(0, self._count - 1)
            logger.info(f"Active viewers: {self._count}")
            
    @property
    def count(self):
        with self._lock:
            return self._count

viewer_counter = ViewerCounter()

class Kick:
    def __init__(self, proxy: Optional[str] = None):
        self.session = self._create_session(proxy)
        
    def _create_session(self, proxy: Optional[str]) -> curl_cffi.Session:
        session = curl_cffi.Session(impersonate="firefox135")
        headers = {
            "sec-ch-ua": "\"Google Chrome\";v=\"131\", \"Not)A;Brand\";v=\"99\", \"Microsoft Edge\";v=\"131\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
        session.headers.update(headers)
        
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
            
        return session
        
    def get_stream_url(self, username: str) -> Optional[str]:
        try:
            response = self.session.get(f"https://kick.com/{username}")
            pattern = r'playback_url\\\":\\\"(https://[^\\\"]+)'
            match = re.search(pattern, response.text)
            
            if match:
                playback_url = match.group(1)
                return playback_url.replace('\\\\', '\\').replace('\\/', '/')
            return None
        except Exception as e:
            logger.error(f"Error getting stream URL: {e}")
            return None

class M3U8Handler:
    def __init__(self, master_url: str, session: Optional[Any] = None):
        self.master_url = master_url
        self.session = session or requests.Session()
        self.base_url = self._get_base_url(master_url)
        self.stop_event = threading.Event()
        self.playback_thread = None
        
    def _get_base_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{os.path.dirname(parsed.path)}/"
    
    def _resolve_url(self, segment_url: str) -> str:
        if segment_url.startswith('http'):
            return segment_url
        return urljoin(self.base_url, segment_url)
    
    def fetch_playlist(self) -> Optional[m3u8.M3U8]:
        try:
            response = self.session.get(self.master_url)
            return m3u8.loads(response.text)
        except Exception as e:
            logger.error(f"Error fetching master playlist: {e}")
            return None
            
    def get_lowest_bandwidth_stream(self) -> Optional[str]:
        playlist = self.fetch_playlist()
        if not playlist:
            return None
            
        if not playlist.playlists:
            return self.master_url
            
        min_bandwidth = float('inf')
        lowest_variant = None
        
        for variant in playlist.playlists:
            bandwidth = variant.stream_info.bandwidth
            if bandwidth < min_bandwidth:
                min_bandwidth = bandwidth
                lowest_variant = variant
                
        if lowest_variant:
            return self._resolve_url(lowest_variant.uri)
        return None
        
    def fetch_media_playlist(self, playlist_url: str) -> Optional[m3u8.M3U8]:
        try:
            response = self.session.get(playlist_url)
            return m3u8.loads(response.text)
        except Exception as e:
            logger.error(f"Error fetching media playlist: {e}")
            return None
            
    def fetch_segment(self, segment_url: str) -> bool:
        full_url = self._resolve_url(segment_url)
        try:
            start_time = time.time()
            response = self.session.get(full_url, stream=True)
            for chunk in response.iter_content(chunk_size=1):
                if chunk:
                    break
            duration = time.time() - start_time
            logger.info(f"Segment fetched in {duration:.2f}s: {full_url}")
            return True
        except Exception as e:
            logger.error(f"Error fetching segment: {e}")
            return False
            
    def simulate_playback(self, media_playlist_url: str) -> None:
        viewer_counter.increment()
        try:
            while not self.stop_event.is_set():
                media_playlist = self.fetch_media_playlist(media_playlist_url)
                if not media_playlist or not media_playlist.segments:
                    break
                    
                for segment in media_playlist.segments[:min(2, len(media_playlist.segments))]:
                    if self.stop_event.is_set():
                        break
                    self.fetch_segment(segment.uri)
                    time.sleep(40)
                if media_playlist.is_endlist:
                    break
                time.sleep(5)
        finally:
            viewer_counter.decrement()
        
    def start(self) -> bool:
        lowest_stream_url = self.get_lowest_bandwidth_stream()
        if not lowest_stream_url:
            logger.error("Could not find a stream to play")
            return False
            
        self.playback_thread = threading.Thread(target=self.simulate_playback, args=(lowest_stream_url,))
        self.playback_thread.daemon = True
        self.playback_thread.start()
        return True
        
    def stop(self) -> None:
        self.stop_event.set()
        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join(timeout=5)

def cleanup_handlers(handlers):
    logger.info("Cleaning up handlers...")
    for handler in handlers:
        handler.stop()

def view_stream(username: str, proxy: Optional[str] = None) -> None:
    kick = Kick(proxy)
    playback_url = kick.get_stream_url(username)
    
    if not playback_url:
        logger.error("Could not find playback URL")
        return
        
    handler = M3U8Handler(playback_url,  session=kick.session)
    if handler.start():
        logger.info("Handler started successfully.")
        return handler
    else:
        logger.error("Failed to start the handler.")
        return None

def main(viewers: int = 1, username: str = "username", proxy: Optional[str] = None):
    active_handlers = []
    
    try:
        with ThreadPoolExecutor(max_workers=1) as executor: ##cloudflare rate limiting
            futures = []
            for _ in range(viewers):
                future = executor.submit(view_stream, username, proxy)
                futures.append(future)
            
            for future in futures:
                handler = future.result()
                if handler:
                    active_handlers.append(handler)
            
            logger.info(f"Successfully started {len(active_handlers)} viewers")
            atexit.register(cleanup_handlers, active_handlers)
            try:
                while True:
                    time.sleep(1)
                    logger.info(f"Current active viewers: {viewer_counter.count}")
                    time.sleep(60)
            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt, shutting down...")
                
    finally:
        cleanup_handlers(active_handlers)

if __name__ == "__main__":
    main(viewers=1000, username="username")
