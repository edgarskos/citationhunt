import chdb
import config
from snippet_parser import CITATION_NEEDED_MARKER, REF_MARKER

import flask
import flask_sslify
from flask.ext.compress import Compress

import os
import contextlib
import collections
from datetime import datetime
import functools
import itertools

# the markup we're going to use for [citation needed] and <ref> tags,
# pre-marked as safe for jinja.
SUPERSCRIPT_HTML = '<sup class="superscript">[%s]</sup>'
SUPERSCRIPT_MARKUP = flask.Markup(SUPERSCRIPT_HTML)
CITATION_NEEDED_MARKUP = flask.Markup(SUPERSCRIPT_HTML % 'citation&thinsp;needed')

@contextlib.contextmanager
def log_time(operation):
    before = datetime.now()
    yield
    after = datetime.now()
    ms = (after - before).microseconds / 1000.
    print '[citationhunt] %s took %.2f ms' % (operation, ms)

def get_db(lang_code):
    localized_dbs = getattr(flask.g, '_localized_dbs', {})
    db = localized_dbs.get(lang_code, None)
    if db is None:
        db = localized_dbs[lang_code] = chdb.init_db(lang_code)
    flask.g._localized_dbs = localized_dbs
    return db

Category = collections.namedtuple('Category', ['id', 'title'])
CATEGORY_ALL = Category('all', '')
def get_categories(lang_code, include_default = True):
    categories = getattr(flask.g, '_categories', None)
    if categories is None:
        cursor = get_db(lang_code).cursor()
        cursor.execute('''
            SELECT id, title FROM categories WHERE id != "unassigned"
            ORDER BY title;
        ''')
        categories = [CATEGORY_ALL] + [Category(*row) for row in cursor]
        flask.g._categories = categories
    return categories if include_default else categories[1:]

def get_category_by_id(lang_code, catid, default = None):
    for c in get_categories(lang_code):
        if catid == c.id:
            return c
    return default

def select_snippet_by_id(lang_code, id):
    cursor = get_db(lang_code).cursor()
    with log_time('select snippet by id'):
        cursor.execute('''
            SELECT snippets.snippet, snippets.section, articles.url,
            articles.title FROM snippets, articles WHERE snippets.id = %s AND
            snippets.article_id = articles.page_id;''', (id,))
        ret = cursor.fetchone()
    return ret

def select_random_id(lang_code, cat = CATEGORY_ALL):
    cursor = get_db(lang_code).cursor()

    ret = None
    if cat is not CATEGORY_ALL:
        with log_time('select with category'):
            cursor.execute('''
                SELECT snippets.id FROM snippets, articles_categories
                WHERE snippets.article_id = articles_categories.article_id AND
                articles_categories.category_id = %s ORDER BY RAND()
                LIMIT 1;''', (cat.id,))
            ret = cursor.fetchone()

    if ret is None:
        # Try to pick one id at random. For small datasets, the probability
        # of getting an empty set in a query is non-negligible, so retry a
        # few times as needed.
        p = '1e-4' if not debug else '1e-2'
        with log_time('select without category'):
            for retry in range(10):
                cursor.execute(
                    'SELECT id FROM snippets WHERE RAND() < %s LIMIT 1;', (p,))
                ret = cursor.fetchone()
                if ret: break

    assert ret and len(ret) == 1
    return ret[0]

def select_next_id(lang_code, curr_id, cat = CATEGORY_ALL):
    cursor = get_db(lang_code).cursor()

    if cat is not CATEGORY_ALL:
        with log_time('select next id'):
            cursor.execute('''
                SELECT next FROM snippets_links WHERE prev = %s
                AND cat_id = %s''', (curr_id, cat.id))
            ret = cursor.fetchone()
            if ret is None:
                # curr_id doesn't belong to the category
                return None
            assert ret and len(ret) == 1
            next_id = ret[0]
    else:
        next_id = curr_id
        for i in range(3): # super paranoid :)
            next_id = select_random_id(lang_code, cat)
            if next_id != curr_id:
                break
    return next_id

def validate_lang_code(handler):
    @functools.wraps(handler)
    def wrapper(lang_code = '', *args, **kwds):
        if lang_code not in config.lang_code_to_config:
            return flask.redirect(
                flask.url_for('citation_hunt', lang_code = 'en',
                    **flask.request.args))
        return handler(lang_code, *args, **kwds)
    return wrapper

app = flask.Flask(__name__)
Compress(app)
debug = 'DEBUG' in os.environ
if not debug:
    flask_sslify.SSLify(app, permanent = True)

@app.route('/')
@validate_lang_code
def index(lang_code):
    pass # nothing to do but validate lang_code

@app.route('/<lang_code>')
@validate_lang_code
def citation_hunt(lang_code):
    id = flask.request.args.get('id')
    cat = flask.request.args.get('cat')

    if cat is not None:
        cat = get_category_by_id(lang_code, cat)
        if cat is None:
            # invalid category, normalize to "all" and try again by id
            cat = CATEGORY_ALL
            return flask.redirect(
                flask.url_for('citation_hunt',
                    lang_code = lang_code, id = id, cat = cat.id))
    else:
        cat = CATEGORY_ALL

    if id is not None:
        sinfo = select_snippet_by_id(lang_code, id)
        if sinfo is None:
            # invalid id
            flask.abort(404)
        snippet, section, aurl, atitle = sinfo
        next_snippet_id = select_next_id(lang_code, id, cat)
        if next_snippet_id is None:
            # the snippet doesn't belong to the category!
            assert cat is not CATEGORY_ALL
            return flask.redirect(
                flask.url_for('citation_hunt',
                    id = id, cat = CATEGORY_ALL.id,
                    lang_code = lang_code))
        return flask.render_template('index.html',
            snippet = snippet, section = section, article_url = aurl,
            article_title = atitle, current_category = cat,
            next_snippet_id = next_snippet_id,
            cn_marker = CITATION_NEEDED_MARKER,
            cn_html = CITATION_NEEDED_MARKUP,
            ref_marker = REF_MARKER,
            ref_html = SUPERSCRIPT_MARKUP,
            lang_code = lang_code)

    id = select_random_id(lang_code, cat)
    return flask.redirect(
        flask.url_for('citation_hunt',
            id = id, cat = cat.id, lang_code = lang_code))

@app.route('/<lang_code>/categories.html')
@validate_lang_code
def categories_html(lang_code):
    response = flask.make_response(
        flask.render_template('categories.html',
            categories = get_categories(lang_code, include_default = False)))
    response.cache_control.max_age = 3 * 60 * 60
    return response

@app.after_request
def add_cache_header(response):
    if response.status_code == 200:
        response.cache_control.public = True
        if response.cache_control.max_age is None:
            response.cache_control.max_age = 24 * 60 * 60
    elif response.status_code == 302:
        response.cache_control.no_cache = True
        response.cache_control.no_store = True
    return response

@app.teardown_appcontext
def close_db(exception):
    db = getattr(flask.g, '_db', None)
    if db is not None:
        db.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host = '0.0.0.0', port = port, debug = debug)
