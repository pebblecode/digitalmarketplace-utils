from __future__ import absolute_import
import os
import re
import boto
import boto.exception
import datetime
import mimetypes
import logging
from dateutil.parser import parse as parse_time

from boto.exception import S3ResponseError  # noqa

from .formats import DATETIME_FORMAT

logger = logging.getLogger(__name__)

FILE_SIZE_LIMIT = 5400000  # approximately 5Mb
BUCKET_SHORT_NAME_PATTERN = re.compile(
    r'^digitalmarketplace-([^\-]+)-([^\-]+)-(\2)$'
)


class S3(object):
    def __init__(self, bucket_name=None, host='s3-eu-west-1.amazonaws.com'):
        conn = boto.connect_s3(host=host)

        self.bucket_name = bucket_name
        self.bucket = conn.get_bucket(bucket_name)

    @property
    def bucket_short_name(self):
        match = BUCKET_SHORT_NAME_PATTERN.match(self.bucket_name)

        if not match:
            raise ValueError("Cannot parse bucket name: {}".format(self.bucket_name))

        return match.group(1)

    def save(self, path, file, acl='public-read', move_prefix=None, timestamp=None, download_filename=None):
        """Save a file in an S3 bucket

        canned ACL list: https://docs.aws.amazon.com/AmazonS3/latest/dev/acl-overview.html#canned-acl

        :param path:        location in S3 bucket at which to save the file
        :param file:        file object to be saved in S3
        :param acl:         S3 canned ACL
        :param move_prefix: Prefix to give to existing file when moving it out of the way
        :param timestamp:   Timestamp to set for this file rather than using utcnow

        :return: S3 Key
        """
        path = path.lstrip('/')

        key = self.bucket.new_key(path)
        filesize = get_file_size_up_to_maximum(file)
        timestamp = timestamp or datetime.datetime.utcnow()
        key.set_metadata('timestamp', timestamp.strftime(DATETIME_FORMAT))
        headers = {'Content-Type': self._get_mimetype(key.name)}
        if download_filename:
            headers['Content-Disposition'] = 'attachment; filename="{}"'.format(download_filename).encode('utf-8')
        key.set_contents_from_file(
            file,
            headers=headers
        )
        key.set_acl(acl)
        logger.info(
            "Uploaded file {filepath} of size {filesize} with acl {fileacl}",
            extra={
                "filepath": path,
                "filesize": filesize,
                "fileacl": acl,
            })

        return key

    def path_exists(self, path):
        return bool(self.bucket.get_key(path))

    def get_signed_url(self, path, expires_in=30):
        """Create a signed S3 document URL

        :param path: S3 object path within the bucket
        :param expires_in: how long the generated URL is valid
                           for, in seconds

        :return: signed URL or ``None`` if object was not found

        """

        key = self.bucket.get_key(path)
        if key:
            return key.generate_url(expires_in)

    def get_key(self, path):
        key = self.bucket.get_key(path)
        if key:
            return self._format_key(key, False, key.get_metadata('timestamp'))

    def delete_key(self, path):
        self.bucket.delete_key(path)

    def list(self, prefix='', delimiter='', load_timestamps=False):
        """
        return a list of file keys (ordered by last_modified date) from an s3 bucket

        Prefix & Delimiter: http://docs.aws.amazon.com/AmazonS3/latest/dev/ListingKeysHierarchy.html
        :param prefix:         filter by files whose names begin with the prefix
        :param delimiter:      filter out files whose names contain the delimiter
        :param load_timestamp: by default custom timestamps are not loaded as they require an extra API call.
                               If you need to show the timestamp set this to True.
        :return: list
        """
        # http://boto.readthedocs.org/en/latest/ref/s3.html#boto.s3.bucket.Bucket.list
        list_of_keys = self.bucket.list(prefix, delimiter)
        return sorted([
            self._format_key(key, load_timestamps)
            for key in list_of_keys
            if not (key.size == 0 and key.name[-1] == '/')
        ], key=lambda key: key['last_modified'])

    def _format_key(self, key, load_timestamps, timestamp=None):
        """
        transform a boto s3 Key object into a (simpler) dict

        :param key:            http://boto.readthedocs.org/en/latest/ref/s3.html#boto.s3.key.Key
        :param load_timestamp: by default custom timestamps are not loaded as they require an extra API call.
                               If you need to show the timestamp set this to True.
        :return:    dict
        """
        filename, ext = os.path.splitext(os.path.basename(key.name))
        if load_timestamps:
            key = self.bucket.get_key(key.name)
            timestamp = key.get_metadata('timestamp')

        timestamp = timestamp or key.last_modified
        timestamp = parse_time(timestamp)

        return {
            'path': key.name,
            'filename': filename,
            'ext': ext[1:],
            'last_modified': timestamp.strftime(DATETIME_FORMAT),
            'size': key.size
        }

    def _get_mimetype(self, filename):
        mimetype, _ = mimetypes.guess_type(filename)
        return mimetype


def get_file_size_up_to_maximum(file_contents):
    size = len(file_contents.read(FILE_SIZE_LIMIT))
    file_contents.seek(0)

    return size
