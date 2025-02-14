from aws_requests_auth.aws_auth import AWSRequestsAuth
from elasticsearch import Elasticsearch, RequestsHttpConnection
from elasticsearch.exceptions import AuthenticationException, \
    AuthorizationException, NotFoundError
from elasticsearch_dsl import Q, Search, connections
from elasticsearch_dsl.response import Response, Hit
from cccatalog import settings
from django.core.cache import cache
from cccatalog.api.models import ContentProvider
from rest_framework import serializers
import logging as log
from cccatalog.settings import THUMBNAIL_PROXY_URL, PROXY_THUMBS, PROXY_ALL
from cccatalog.api.utils.validate_images import validate_images
from cccatalog.api.utils.dead_link_mask import get_query_mask, get_query_hash
from rest_framework.reverse import reverse
from itertools import accumulate
from typing import Tuple, List, Optional
from math import ceil

ELASTICSEARCH_MAX_RESULT_WINDOW = 10000
CACHE_TIMEOUT = 10
DEAD_LINK_RATIO = 1 / 2
THUMBNAIL = 'thumbnail'
URL = 'url'
THUMBNAIL_WIDTH_PX = 600
PROVIDER = 'provider'


def _paginate_with_dead_link_mask(s: Search, page_size: int,
                                  page: int) -> Tuple[int, int]:
    """
    Given a query, a page and pagesize, it returns the start and end
    of the slice of results.

    :param s: The elasticsearch Search object
    :param page_size: How big the page should be.
    :param page: The page number.
    :return: Tuple of start and end.
    """
    query_hash = get_query_hash(s)
    query_mask = get_query_mask(query_hash)
    if not query_mask:
        start = 0
        end = ceil(page_size * page / (1 - DEAD_LINK_RATIO))
    elif page_size * (page - 1) > sum(query_mask):
        start = len(query_mask)
        end = ceil(page_size * page / (1 - DEAD_LINK_RATIO))
    else:
        accu_query_mask = list(accumulate(query_mask))
        start = 0
        if page > 1:
            try:
                start = accu_query_mask.index(page_size * (page - 1) + 1)
            except ValueError:
                start = accu_query_mask.index(page_size * (page - 1)) + 1
        if page_size * page > sum(query_mask):
            end = ceil(page_size * page / (1 - DEAD_LINK_RATIO))
        else:
            end = accu_query_mask.index(page_size * page) + 1
    return start, end


def _get_query_slice(s: Search, page_size: int, page: int,
                     filter_dead: Optional[bool] = False) -> Tuple[int, int]:
    """
    Select the start and end of the search results for this query.
    """
    if filter_dead:
        start_slice, end_slice = \
            _paginate_with_dead_link_mask(s, page_size, page)
    else:
        # Paginate search query.
        start_slice = page_size * (page - 1)
        end_slice = page_size * page
    if start_slice + end_slice > ELASTICSEARCH_MAX_RESULT_WINDOW:
        raise ValueError("Deep pagination is not allowed.")
    return start_slice, end_slice


def _filter_licenses(s: Search, licenses):
    """
    Filter out all licenses except for those provided in the `licenses`
    parameter.
    """
    if not licenses:
        return s
    license_filters = []
    for _license in licenses.split(','):
        license_filters.append(Q('term', license__keyword=_license))
    s = s.filter('bool', should=license_filters, minimum_should_match=1)
    return s


def _quote_escape(query_string):
    """
    If there are any unmatched quotes in the query supplied by the user, ignore
    them.
    """
    num_quotes = query_string.count('"')
    if num_quotes % 2 == 1:
        return query_string.replace('"', '\\"')
    else:
        return query_string


def _post_process_results(s, start, end, page_size, search_results,
                          request, filter_dead) -> List[Hit]:
    """
    After fetching the search results from the back end, iterate through the
    results, add links to detail views, perform image validation, and route
    certain thumbnails through out proxy.

    :param s: The Elasticsearch Search object.
    :param start: The start of the result slice.
    :param end: The end of the result slice.
    :param search_results: The Elasticsearch response object containing search
    results.
    :param request: The Django request object, used to build a "reversed" URL
    to detail pages.
    :param filter_dead: Whether images should be validated.
    :return: List of results.
    """
    results = []
    to_validate = []
    for res in search_results:
        url = request.build_absolute_uri(
            reverse('image-detail', [res.identifier])
        )
        res.detail = url
        if hasattr(res.meta, 'highlight'):
            res.fields_matched = dir(res.meta.highlight)
        to_validate.append(res.url)
        if PROXY_THUMBS:
            # Proxy thumbnails from providers who don't provide SSL. We also
            # have a list of providers that have poor quality or no thumbnails,
            # so we produce our own on-the-fly.
            provider = res[PROVIDER]
            if THUMBNAIL in res and provider not in PROXY_ALL:
                to_proxy = THUMBNAIL
            else:
                to_proxy = URL
            if 'http://' in res[to_proxy] or provider in PROXY_ALL:
                original = res[to_proxy]
                secure = '{proxy_url}/{width}/{original}'.format(
                    proxy_url=THUMBNAIL_PROXY_URL,
                    width=THUMBNAIL_WIDTH_PX,
                    original=original
                )
                res[THUMBNAIL] = secure
        results.append(res)

    if filter_dead:
        query_hash = get_query_hash(s)
        validate_images(query_hash, start, results, to_validate)

        if len(results) < page_size:
            end += int(end / 2)
            if start + end > ELASTICSEARCH_MAX_RESULT_WINDOW:
                return results

            s = s[start:end]
            search_response = s.execute()

            return _post_process_results(
                s,
                start,
                end,
                page_size,
                search_response,
                request,
                filter_dead
            )
    return results[:page_size]


def search(search_params, index, page_size, ip, request,
           filter_dead, page=1) -> Tuple[List[Hit], int, int]:
    """
    Given a set of keywords and an optional set of filters, perform a ranked
    paginated search.

    :param search_params: Search parameters. See
     :class: `ImageSearchQueryStringSerializer`.
    :param index: The Elasticsearch index to search (e.g. 'image')
    :param page_size: The number of results to return per page.
    :param ip: The user's hashed IP. Hashed IPs are used to anonymously but
    uniquely identify users exclusively for ensuring query consistency across
    Elasticsearch shards.
    :param request: Django's request object.
    :param filter_dead: Whether dead links should be removed.
    :param page: The results page number.
    :return: Tuple with a List of Hits from elasticsearch, the total count of
    pages and results.
    """
    s = Search(index=index)
    # Add requested filters.
    if 'li' in search_params.data:
        s = _filter_licenses(s, search_params.data['li'])
    elif 'lt' in search_params.data:
        s = _filter_licenses(s, search_params.data['lt'])

    if 'provider' in search_params.data:
        provider_filters = []
        for provider in search_params.data['provider'].split(','):
            provider_filters.append(Q('term', provider=provider))
        s = s.filter('bool', should=provider_filters, minimum_should_match=1)
    if 'extension' in search_params.data:
        extension = search_params.data['extension']
        extension_filter = Q('term', extension=extension)
        s = s.filter('bool', should=extension_filter, minimum_should_match=1)

    # It is sometimes desirable to hide content providers from the catalog
    # without scrubbing them from the database or reindexing.
    filter_cache_key = 'filtered_providers'
    filtered_providers = cache.get(key=filter_cache_key)
    if not filtered_providers:
        filtered_providers = ContentProvider.objects\
            .filter(filter_content=True)\
            .values('provider_identifier')
        cache.set(
            key=filter_cache_key,
            timeout=CACHE_TIMEOUT,
            value=filtered_providers
        )
    for filtered in filtered_providers:
        s = s.exclude('match', provider=filtered['provider_identifier'])

    # Search either by generic multimatch or by "advanced search" with
    # individual field-level queries specified.
    search_fields = ['tags.name', 'title', 'description']
    if 'q' in search_params.data:
        query = _quote_escape(search_params.data['q'])
        s = s.query(
            'query_string',
            query=query,
            fields=search_fields,
            type='most_fields'
        )
    else:
        if 'creator' in search_params.data:
            creator = _quote_escape(search_params.data['creator'])
            s = s.query(
                'query_string', query=creator, default_field='creator'
            )
        if 'title' in search_params.data:
            title = _quote_escape(search_params.data['title'])
            s = s.query(
                'query_string', query=title, default_field='title'
            )
        if 'tags' in search_params.data:
            tags = _quote_escape(search_params.data['tags'])
            s = s.query(
                'query_string',
                default_field='tags.name',
                query=tags
            )

    # Use highlighting to determine which fields contribute to the selection of
    # top results.
    s = s.highlight(*search_fields)
    s = s.highlight_options(order='score')
    s.extra(track_scores=True)
    # Route users to the same Elasticsearch worker node to reduce
    # pagination inconsistencies and increase cache hits.
    s = s.params(preference=str(ip))
    # Paginate
    start, end = _get_query_slice(s, page_size, page, filter_dead)
    s = s[start:end]
    search_response = s.execute()
    results = _post_process_results(
        s,
        start,
        end,
        page_size,
        search_response,
        request,
        filter_dead
    )

    result_count, page_count = _get_result_and_page_count(
        search_response,
        results,
        page_size
    )

    return results, page_count, result_count


def _validate_provider(input_provider):
    allowed_providers = list(get_providers('image').keys())
    lowercase_providers = [x.lower() for x in allowed_providers]
    if input_provider.lower() not in lowercase_providers:
        raise serializers.ValidationError(
            "Provider \'{}\' does not exist.".format(input_provider)
        )
    return input_provider.lower()


def related_images(uuid, index, request, filter_dead):
    """
    Given a UUID, find related search results.
    """
    # Convert UUID to sequential ID.
    item = Search(index=index)
    item = item.query(
        'match',
        identifier=uuid
    )
    _id = item.execute().hits[0].id

    s = Search(index=index)
    s = s.query(
        'more_like_this',
        fields=['tags.name', 'title', 'creator'],
        like={
            '_index': index,
            '_id': _id
        },
        min_term_freq=1,
        max_query_terms=50
    )
    page_size = 10
    page = 1
    start, end = _get_query_slice(s, page_size, page, filter_dead)
    s = s[start:end]
    response = s.execute()
    results = _post_process_results(
        s,
        start,
        end,
        page_size,
        response,
        request,
        filter_dead
    )

    result_count, _ = _get_result_and_page_count(
        response,
        results,
        page_size
    )

    return results, result_count


def browse_by_provider(provider, index, page_size, ip, request, filter_dead,
                       page=1, lt=None, li=None):
    """
    Allow users to browse image collections without entering a search query.
    """
    _validate_provider(provider)
    s = Search(index=index)
    s = s.params(preference=str(ip))
    provider_filter = Q('term', provider=provider.lower())
    s = s.filter('bool', should=provider_filter, minimum_should_match=1)
    licenses = lt if lt else li
    s = _filter_licenses(s, licenses)
    start_slice, end_slice = _get_query_slice(s, page_size, page)
    s = s[start_slice:end_slice]
    search_response = s.execute()
    results = _post_process_results(
        s,
        start_slice,
        end_slice,
        page_size,
        search_response,
        request,
        filter_dead
    )

    result_count, page_count = _get_result_and_page_count(
        search_response,
        results,
        page_size
    )

    return results, page_count, result_count


def get_providers(index):
    """
    Given an index, find all available data providers and return their counts.

    :param index: An Elasticsearch index, such as `'image'`.
    :return: A dictionary mapping providers to the count of their images.`
    """
    provider_cache_name = 'providers-' + index
    providers = cache.get(key=provider_cache_name)
    if type(providers) == list:
        # Invalidate old provider format.
        cache.delete(key=provider_cache_name)
    if not providers:
        elasticsearch_maxint = 2147483647
        agg_body = {
            'aggs': {
                'unique_providers': {
                    'terms': {
                        'field': 'provider.keyword',
                                 'size': elasticsearch_maxint,
                        "order": {
                            "_key": "desc"
                        }
                    }
                }
            }
        }
        s = Search.from_dict(agg_body)
        s = s.index(index)
        try:
            results = s.execute().aggregations['unique_providers']['buckets']
        except NotFoundError:
            results = [{'key': 'none_found', 'doc_count': 0}]
        providers = {result['key']: result['doc_count'] for result in results}
        cache.set(
            key=provider_cache_name,
            timeout=CACHE_TIMEOUT,
            value=providers
        )
    return providers


def _elasticsearch_connect():
    """
    Connect to configured Elasticsearch domain.

    :return: An Elasticsearch connection object.
    """
    try:
        log.info('Trying to connect to Elasticsearch without authentication...')
        # Try to connect to Elasticsearch without credentials.
        _es = Elasticsearch(
            host=settings.ELASTICSEARCH_URL,
            port=settings.ELASTICSEARCH_PORT,
            connection_class=RequestsHttpConnection,
            timeout=10,
            max_retries=99,
            wait_for_status='yellow'
        )
        log.info(str(_es.info()))
        log.info('Connected to Elasticsearch without authentication.')
    except (AuthenticationException, AuthorizationException):
        # If that fails, supply AWS authentication object and try again.
        log.info(
            'Connecting to %s %s with AWS auth', settings.ELASTICSEARCH_URL,
            settings.ELASTICSEARCH_PORT)
        auth = AWSRequestsAuth(
            aws_access_key=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            aws_host=settings.ELASTICSEARCH_URL,
            aws_region=settings.ELASTICSEARCH_AWS_REGION,
            aws_service='es'
        )
        auth.encode = lambda x: bytes(x.encode('utf-8'))
        _es = Elasticsearch(
            host=settings.ELASTICSEARCH_URL,
            port=settings.ELASTICSEARCH_PORT,
            connection_class=RequestsHttpConnection,
            timeout=10,
            max_retries=99,
            retry_on_timeout=True,
            http_auth=auth,
            wait_for_status='yellow'
        )
        _es.info()
    return _es


es = _elasticsearch_connect()
connections.connections.add_connection('default', es)


def _get_result_and_page_count(response_obj: Response, results: List[Hit],
                               page_size: int) -> Tuple[int, int]:
    """
    Elasticsearch does not allow deep pagination of ranked queries.
    Adjust returned page count to reflect this.

    :param response_obj: The original Elasticsearch response object.
    :param results: The list of filtered result Hits.
    :return: Result and page count.
    """
    result_count = response_obj.hits.total
    natural_page_count = int(result_count / page_size)
    last_allowed_page = int((5000 + page_size / 2) / page_size)
    page_count = min(natural_page_count, last_allowed_page)
    if len(results) < page_size and page_count == 0:
        result_count = len(results)

    return result_count, page_count
