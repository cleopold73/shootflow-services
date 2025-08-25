from shootflow.flickr.flickr import FlickrAPIClient
from datetime import datetime, timedelta

client = FlickrAPIClient()

ret = client.get_photo_score("54729097295")
print(ret)

ret = client.get_download_url("54729097295")
print(ret)
# https://www.flickr.com/photo_download.gne?size=o&id=54729097295&secret=20da187ee3