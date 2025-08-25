from shootflow.flickr.flickr import FlickrAPIClient
from datetime import datetime, timedelta

client = FlickrAPIClient()

ret = client.get_photo_score("54729097295")
print(ret)