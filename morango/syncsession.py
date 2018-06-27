import json
import requests
import socket
import time
import uuid
import logging

from math import ceil
from django.conf import settings
from django.utils import timezone
from django.utils.six import iteritems, moves
from morango.api.serializers import BufferSerializer, CertificateSerializer, InstanceIDSerializer
from morango.certificates import Certificate, Key, Filter
from morango.constants import api_urls, transfer_status
from morango.errors import CertificateSignatureInvalid, MorangoError
from morango.models import Buffer, InstanceIDModel, RecordMaxCounterBuffer, SyncSession, TransferSession, DatabaseMaxCounter
from morango.utils.sync_utils import _serialize_into_store, _queue_into_buffer, _dequeue_into_store
from six.moves.urllib.parse import urljoin, urlparse

from django.core.paginator import Paginator

logger = logging.getLogger(__name__)


def _join_with_logical_operator(lst, operator):
    op = ") {operator} (".format(operator=operator)
    return "(({items}))".format(items=op.join(lst))

def _get_server_ip(hostname):
    try:
        return socket.gethostbyname(hostname)
    except:
        return ''

def _get_client_ip_for_server(server_host, server_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((server_host, server_port))
        IP = s.getsockname()[0]
    except:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


class Connection(object):
    """
    Abstraction around a connection with a syncing peer (network or disk),
    supporting interactions with that peer. This may be used by a SyncClient,
    but also supports other operations (e.g. querying certificates) outside
    the context of syncing.

    This class should be subclassed for particular transport mechanisms,
    and the necessary methods overridden.
    """
    pass


class NetworkSyncConnection(Connection):

    def __init__(self, base_url=''):
        self.base_url = base_url

    def _request(self, endpoint, method="GET", lookup=None, data={}, params={}, userargs=None, password=None, timeout=3, max_retries=5):
        """
        Generic request method designed to handle any morango endpoint.

        :param endpoint: constant representing which morango endpoint we are querying
        :param method: HTTP verb/method for request
        :param lookup: the pk value for the specific object we are querying
        :param data: dict that will be form-encoded in request
        :param params: dict to be sent as part of URL's query string
        :param userargs: Authorization credentials
        :param password:
        :return: ``Response`` object from request
        """
        request_exceptions = (
            requests.exceptions.ConnectionError,
        )

        # convert user arguments into query str for passing to auth layer
        if isinstance(userargs, dict):
            userargs = "&".join(["{}={}".format(key, val) for (key, val) in iteritems(userargs)])

        # build up url and send request
        if lookup:
            lookup = lookup + '/'
        url = urljoin(urljoin(self.base_url, endpoint), lookup)
        auth = (userargs, password) if userargs else None
        # handle network failures and retry logic
        for i in range(max_retries):
            try:
                resp = requests.request(method, url, json=data, params=params, auth=auth)
                # if any other status code besides 2XX, raise an exception and close the transfer session
                resp.raise_for_status()
            except request_exceptions:
                time.sleep(timeout*i)
                continue
            else:
                return resp
        else:
            # raise error if there has been multiple connection errors
            raise requests.exceptions.ConnectionError

    def create_sync_session(self, client_cert, server_cert, chunk_size=500):
        active = SyncSession.objects.filter(active=True).filter(client_certificate=client_cert).filter(server_certificate=server_cert).filter(is_server=False)
        # hoping there should only be one
        if active:
            return SyncClient(self, active[0], chunk_size=chunk_size)

        # if server cert does not exist locally, retrieve it from server
        if not Certificate.objects.filter(id=server_cert.id).exists():
            self._get_certificate_chain(server_cert)

        # request the server for a one-time-use nonce
        nonce_resp = self._request(api_urls.NONCE, method="POST")
        nonce = json.loads(nonce_resp.content.decode())["id"]

        # if no hostname then url is actually an ip
        url = urlparse(self.base_url)
        hostname = url.hostname or self.base_url
        port = url.port or (80 if url.scheme == 'http' else 443)
        # prepare the data to send in the syncsession creation request
        data = {
            "id": uuid.uuid4().hex,
            "server_certificate_id": server_cert.id,
            "client_certificate_id": client_cert.id,
            "profile": client_cert.profile,
            "certificate_chain": json.dumps(CertificateSerializer(client_cert.get_ancestors(include_self=True), many=True).data),
            "connection_path": self.base_url,
            "instance": json.dumps(InstanceIDSerializer(InstanceIDModel.get_or_create_current_instance()[0]).data),
            "nonce": nonce,
            "client_ip": _get_client_ip_for_server(hostname, port),
            "server_ip": _get_server_ip(hostname),
        }

        # sign the nonce/ID combo to attach to the request
        message = "{nonce}:{id}".format(**data)
        data["signature"] = client_cert.sign(message)

        # Sync Session creation request
        session_resp = self._request(api_urls.SYNCSESSION, method="POST", data=data)

        # check that the nonce/id were properly signed by the server cert
        if not server_cert.verify(message, session_resp.json().get("signature")):
            raise CertificateSignatureInvalid()

        # build the data to be used for creating our own syncsession
        data = {
            "id": data['id'],
            "start_timestamp": timezone.now(),
            "last_activity_timestamp": timezone.now(),
            "active": True,
            "is_server": False,
            "client_certificate": client_cert,
            "server_certificate": server_cert,
            "profile": client_cert.profile,
            "connection_kind": "network",
            "connection_path": self.base_url,
            "client_ip": data['client_ip'],
            "server_ip": data['server_ip'],
            "client_instance": json.dumps(InstanceIDSerializer(InstanceIDModel.get_or_create_current_instance()[0]).data),
            "server_instance": session_resp.json().get("server_instance") or "{}",
        }
        sync_session = SyncSession.objects.create(**data)

        return SyncClient(self, sync_session, chunk_size=chunk_size)

    def get_remote_certificates(self, primary_partition, scope_def_id=None):
        remote_certs = []
        # request certs for this primary partition, where the server also has a private key for
        remote_certs_resp = self._request(api_urls.CERTIFICATE, params={'primary_partition': primary_partition})

        # inflate remote certs into a list of unsaved models
        for cert in remote_certs_resp.json():
            remote_certs.append(Certificate.deserialize(cert["serialized"], cert["signature"]))

        # filter certs by scope definition id, if provided
        if scope_def_id:
            remote_certs = [cert for cert in remote_certs if cert.scope_definition_id == scope_def_id]

        return remote_certs

    def certificate_signing_request(self, parent_cert, scope_definition_id, scope_params, userargs=None, password=None):
        # if server cert does not exist locally, retrieve it from server
        if not Certificate.objects.filter(id=parent_cert.id).exists():
            self._get_certificate_chain(parent_cert)

        csr_key = Key()
        # build up data for csr
        data = {
            "parent": parent_cert.id,
            "profile": parent_cert.profile,
            "scope_definition": scope_definition_id,
            "scope_version": parent_cert.scope_version,
            "scope_params": json.dumps(scope_params),
            "public_key": csr_key.get_public_key_string()
        }
        csr_resp = self._request(api_urls.CERTIFICATE, method="POST", data=data, userargs=userargs, password=password)
        csr_data = csr_resp.json()

        # verify cert returned from server, and proceed to save into our records
        csr_cert = Certificate.deserialize(csr_data["serialized"], csr_data["signature"])
        csr_cert.private_key = csr_key
        csr_cert.check_certificate()
        csr_cert.save()
        return csr_cert

    def _get_certificate_chain(self, server_cert):
        # get ancestors certificate chain for this server cert
        cert_chain_resp = self._request(api_urls.CERTIFICATE, params={'ancestors_of': server_cert.id})

        # upon receiving cert chain from server, we attempt to save the chain into our records
        Certificate.save_certificate_chain(cert_chain_resp.json(), expected_last_id=server_cert.id)

    def _create_transfer_session(self, data):
        # create transfer session on server
        return self._request(api_urls.TRANSFERSESSION, method="POST", data=data)

    def _update_transfer_session(self, data, transfer_session):
        # update transfer session on server side with kwargs
        return self._request(api_urls.TRANSFERSESSION, method="PATCH", lookup=transfer_session.id, data=data)

    def _close_transfer_session(self, transfer_session):
        # "delete" transfer session on server side
        return self._request(api_urls.TRANSFERSESSION, method="DELETE", lookup=transfer_session.id)

    def _close_sync_session(self, sync_session):
        # "delete" sync session on server side
        return self._request(api_urls.SYNCSESSION, method="DELETE", lookup=sync_session.id)

    def _push_record_chunk(self, serialized_recs):
        # push a chunk of records to the server side
        return self._request(api_urls.BUFFER, method="POST", data=serialized_recs)

    def _pull_record_chunk(self, chunk_size, transfer_session):
        # pull records from server for given transfer session
        params = {'limit': chunk_size, 'offset': transfer_session.records_transferred, 'transfer_session_id': transfer_session.id}
        return self._request(api_urls.BUFFER, params=params)


class SyncClient(object):
    """
    Controller to support client in initiating syncing and performing related operations.
    """
    def __init__(self, sync_connection, sync_session, chunk_size):
        self.sync_connection = sync_connection
        self.sync_session = sync_session
        self.current_transfer_session = None
        if chunk_size % 100 != 0:
            raise MorangoError('Chunk size must be evenly divisible by 100.')
        self.chunk_size = chunk_size

    def _starting(self, sync_filter, push):
        data = None
        # syncsession may or may not have created a transfer session
        transfer_syncs = self.sync_session.transfersession_set.filter(filter=sync_filter).filter(active=True).filter(push=push)
        if push:
            if transfer_syncs:
                logger.info("Resuming sync push...")
                # grab active transfer session
                self.current_transfer_session = transfer_syncs[0]
                # turn off any other active transfer sessions attached to this syncsession
                self.sync_session.transfersession_set.filter(active=True).exclude(id=self.current_transfer_session.id).update(active=False)
                # clear buffered records from other transfer sessions
                Buffer.objects.delete(transfersession_id__in=self.sync_session.transfersession_set.exclude(id=self.current_transfer_session.id).values_list('id', flat=True))
            else:
                logger.info('Beginning sync push...')
                data = self._generate_transfer_session_data(True, sync_filter)
                data.pop('last_activity_timestamp')
                # create transfer session server side
                try:
                    response = self.sync_connection._create_transfer_session(data)
                except requests.HTTPError as e:
                    self.current_transfer_session.active = False
                    self.current_transfer_session.save()
                    raise

                # create transfer session locally
                data['server_fsic'] = response.json().get('server_fsic') or '{}'
                data['last_activity_timestamp'] = timezone.now()
                data['transfer_stage'] = transfer_status.QUEUING
                self.current_transfer_session = TransferSession.objects.create(**data)
        else:
            if transfer_syncs:
                logger.info("Resuming sync pull...")
                self.current_transfer_session = transfer_syncs[0]
                data = {
                    'id': self.current_transfer_session.id,
                    'filter': self.current_transfer_session.filter,
                    'push': self.current_transfer_session.push,
                    'sync_session_id': self.current_transfer_session.sync_session.id,
                    'transfer_stage': transfer_status.QUEUING,
                    'client_fsic': self.current_transfer_session.client_fsic,
                }
            else:
                logger.info('Beginning sync pull...')
                # create transfer session locally
                data = self._generate_transfer_session_data(False, sync_filter)
                data['last_activity_timestamp'] = timezone.now()
                data['transfer_stage'] = transfer_status.QUEUING
                self.current_transfer_session = TransferSession.objects.create(**data)
                data.pop('last_activity_timestamp')

        return data

    def _queuing(self, data, push):
        if push:
            self._queue_into_buffer()
            # update the records_total for client and server transfer session
            records_total = Buffer.objects.filter(transfer_session=self.current_transfer_session).count()
            self.current_transfer_session.records_total = records_total
            self.current_transfer_session.transfer_stage = transfer_status.PUSHING
            self.current_transfer_session.save()
        else:
            # creating transfer session on pull also queues data server side
            try:
                response = self.sync_connection._create_transfer_session(data)
            except requests.HTTPError as e:
                self.current_transfer_session.active = False
                self.current_transfer_session.save()
                raise

            self.current_transfer_session.server_fsic = response.json().get('server_fsic') or '{}'
            self.current_transfer_session.records_total = response.json().get('records_total')
            self.current_transfer_session.transfer_stage = transfer_status.PULLING
            self.current_transfer_session.save()

    def _pushing(self):
        try:
            self.sync_connection._update_transfer_session({'records_total': self.current_transfer_session.records_total},
                                                          self.current_transfer_session)
        except requests.HTTPError as e:
            self._close_transfer_session()
            raise
        # push records to server
        self._push_records()

        # upon successful completion of pushing records, proceed to delete buffered records
        Buffer.objects.filter(transfer_session=self.current_transfer_session).delete()
        RecordMaxCounterBuffer.objects.filter(transfer_session=self.current_transfer_session).delete()
        self.current_transfer_session.transfer_stage = transfer_status.DEQUEUING
        self.current_transfer_session.save()

    def _pulling(self):
        # pull records and close transfer session upon completion
        try:
            self._pull_records()
        except requests.HTTPError as e:
            self._close_transfer_session()
            raise
        self.current_transfer_session.transfer_stage = transfer_status.DEQUEUING
        self.current_transfer_session.save()

    def _dequeuing(self, push):
        if push:
            # close client and server transfer session
            # closing server transfer session triggers a dequeue
            self._close_transfer_session()
        else:
            self._dequeue_into_store()

    def initiate_push(self, sync_filter):
        data = self._starting(sync_filter, push=True)
        if self.current_transfer_session.transfer_stage == transfer_status.QUEUING:
            logger.info('Preparing records for transfer...')
            self._queuing(data, push=True)

        if self.current_transfer_session.records_total == 0:
            self._close_transfer_session()
            return

        if self.current_transfer_session.transfer_stage == transfer_status.PUSHING:
            logger.info('Pushing {} records to server...'.format(self.current_transfer_session.records_total))
            self._pushing()

        if self.current_transfer_session.transfer_stage == transfer_status.DEQUEUING:
            logger.info('Server is deserializing records...')
            self._dequeuing(push=True)

    def initiate_pull(self, sync_filter):
        data = self._starting(sync_filter, push=False)

        if self.current_transfer_session.transfer_stage == transfer_status.QUEUING:
            logger.info('Server is preparing records for transfer...')
            self._queuing(data, push=False)

        if self.current_transfer_session.records_total == 0:
            self._close_transfer_session()
            return

        if self.current_transfer_session.transfer_stage == transfer_status.PULLING:
            logger.info('Pulling {} records from server...'.format(self.current_transfer_session.records_total))
            self._pulling()

        if self.current_transfer_session.transfer_stage == transfer_status.DEQUEUING:
            logger.info('Deserializing records...')
            self._dequeuing(push=False)

        # update database max counters but use latest fsics on client
        DatabaseMaxCounter.update_fsics(json.loads(self.current_transfer_session.server_fsic),
                                        sync_filter)

        logger.info('Closing session...')
        self._close_transfer_session()

    def _pull_records(self, callback=None):
        while self.current_transfer_session.records_transferred < self.current_transfer_session.records_total:
            logger.info('Pulling {} records at a time with {}/{} records transferred'.format(self.chunk_size, self.current_transfer_session.records_transferred, self.current_transfer_session.records_total))
            try:
                buffers_resp = self.sync_connection._pull_record_chunk(self.chunk_size, self.current_transfer_session)
            except requests.HTTPError as e:
                self._close_transfer_session()
                raise

            # load the returned data from JSON
            data = buffers_resp.json()

            # parse out the results from a paginated set, if needed
            if isinstance(data, dict) and "results" in data:
                data = data["results"]

            # deserialize the records
            serialized_recs = BufferSerializer(data=data, many=True)

            # validate records
            if serialized_recs.is_valid(raise_exception=True):
                serialized_recs.save()

            # update the size of the records transferred
            self.current_transfer_session.records_transferred += self.chunk_size
            self.current_transfer_session.save()

    def _push_records(self, callback=None):
        # paginate buffered records so we do not load them all into memory
        # order by to get the records in the same order every time
        buffered_records = Buffer.objects.filter(transfer_session=self.current_transfer_session).order_by('pk')
        buffered_pages = Paginator(buffered_records, self.chunk_size)

        # if we change the chunk size, we want to reflect that in the pages being sent
        page_number = int(ceil(self.current_transfer_session.records_transferred / float(self.chunk_size))) + 1
        page_range = moves.range(page_number, buffered_pages.num_pages + 1)

        for count in page_range:
            # serialize and send records to server
            serialized_recs = BufferSerializer(buffered_pages.page(count).object_list, many=True)
            logger.info('Pushing {} records at a time with {}/{} records transferred'.format(self.chunk_size, self.current_transfer_session.records_transferred, self.current_transfer_session.records_total))
            try:
                self.sync_connection._push_record_chunk(serialized_recs.data)
            except requests.HTTPError as e:
                self._close_transfer_session()
                raise

            # update records_transferred upon successful request
            self.current_transfer_session.records_transferred += self.chunk_size
            self.current_transfer_session.save()

    def close_sync_session(self):

        # "delete" sync session on server side
        self.sync_connection._close_sync_session(self.sync_session)

        # "delete" our own local sync session
        if self.current_transfer_session is not None:
            raise MorangoError('Transfer Session must be closed before closing sync session.')
        self.sync_session.active = False
        self.sync_session.save()
        self.sync_session = None

    def _generate_transfer_session_data(self, push, filter):
        # build data for creating transfer session on server side
        data = {
            'id': uuid.uuid4().hex,
            'filter': str(filter),
            'push': push,
            'sync_session_id': self.sync_session.id,
        }

        if push:
            # before pushing, we want to serialize the most recent data and update database max counters
            if getattr(settings, 'MORANGO_SERIALIZE_BEFORE_QUEUING', True):
                _serialize_into_store(self.sync_session.profile, filter=filter)

        data['last_activity_timestamp'] = timezone.now()

        data['client_fsic'] = json.dumps(DatabaseMaxCounter.calculate_filter_max_counters(filter))
        return data

    def _close_transfer_session(self):

        # "delete" transfer session on server side
        try:
            self.sync_connection._close_transfer_session(self.current_transfer_session)
        except requests.HTTPError as e:
            self.current_transfer_session.active = False
            self.current_transfer_session.save()
            raise

        # "delete" our own local transfer session
        self.current_transfer_session.active = False
        self.current_transfer_session.transfer_stage = transfer_status.COMPLETED
        self.current_transfer_session.save()
        self.current_transfer_session = None

    def _queue_into_buffer(self):
        _queue_into_buffer(self.current_transfer_session)

    def _dequeue_into_store(self):
        """
        Takes data from the buffers and merges into the store and record max counters.
        """
        _dequeue_into_store(self.current_transfer_session)
