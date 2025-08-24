from shootflow.flickr.flickr import FlickrAPIClient
from datetime import datetime, timedelta

client = FlickrAPIClient()

for item in client.iter_public_photos_date_range(start_date=datetime.now() - timedelta(days=365*4), end_date=datetime.now()):
    print(item)