# Python
import logging
import requests

from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.flickr import FlickrAPIClient
from shootflow.shared.logger import setup_basic_logging

logger = logging.getLogger(__name__)


def main():
    setup_basic_logging()
    flickr_client = FlickrAPIClient()
    db = PhotoRepository()

    photo_id, flickr_id = db.get_photo_with_highest_flickr_score()
    url = flickr_client.get_download_url(flickr_id)
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(f"{flickr_id}.jpg", "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:                  # skip keep-alive chunks
                    f.write(chunk)
    db.update_instagram_posted(photo_id, "POSTED")


if __name__ == "__main__":
    main()