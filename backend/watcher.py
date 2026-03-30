import os
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from ingest import process_file

WATCH_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "brain_data")

class BrainHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            print(f"New file detected: {event.src_path}")
            # Wait to ensure file is fully copied before processing
            time.sleep(1.5) 
            process_file(event.src_path)

def run_watcher():
    os.makedirs(WATCH_DIR, exist_ok=True)
    event_handler = BrainHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=False)
    observer.start()
    print(f"Watching {WATCH_DIR} for new files to ingest...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nWatcher stopped.")
    observer.join()

if __name__ == "__main__":
    run_watcher()
