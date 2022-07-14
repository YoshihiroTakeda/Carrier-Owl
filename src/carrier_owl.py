# from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from selenium import webdriver
# from selenium.webdriver.chrome.options import Options
from selenium.webdriver.firefox.options import Options
import os
import logging
import re
import time
import yaml
import datetime
import holidays
import pytz
import slackweb
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import argparse
import textwrap
from bs4 import BeautifulSoup
import warnings
import urllib.parse
from dataclasses import dataclass
import arxiv
import requests
import yaml

from get_mention_dict import get_mention_dict
# setting
warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)


@dataclass
class Result:
    url: str
    title: str
    en_title: str
    abstract: str
    en_abstract: str
    words: list
    score: float = 0.0


def calc_score(abst: str, keywords: dict) -> (float, list):
    sum_score = 0.0
    hit_kwd_list = []

    for word in keywords.keys():
        score = keywords[word]
        if word.lower() in abst.lower():
            sum_score += score
            hit_kwd_list.append(word)
    return sum_score, hit_kwd_list


def search_keyword(
        articles: list, keywords: dict, score_threshold: float
        ) -> list:
    results = []
    
    # ヘッドレスモードでブラウザを起動
    options = Options()
    options.add_argument('--headless')

    # ブラウザーを起動
    #     driver = webdriver.Chrome(ChromeDriverManager().install(), options=options)
    driver = webdriver.Firefox(executable_path=GeckoDriverManager().install(), options=options)
    
    for article in articles:
        url = article['arxiv_url']
        title = article['title']
        abstract = article['summary']
        score, hit_keywords = calc_score(abstract, keywords)
        if score >= score_threshold:
            title = title.replace('\n', ' ')
            title_trans = get_translated_text('ja', 'en', title, driver)
            abstract = abstract.replace('\n', ' ')
            abstract_trans = get_translated_text('ja', 'en', abstract, driver)
#             abstract_trans = textwrap.wrap(abstract_trans, 40)  # 40行で改行
#             abstract_trans = '\n'.join(abstract_trans)
            result = Result(
                    url=url, title=title_trans, en_title=title, abstract=abstract_trans, en_abstract=abstract,
                    score=score, words=hit_keywords)
            results.append(result)
#         break  # debug

    # ブラウザ停止
    driver.quit()
    
    return results


def mask(labels, text):
    def _make_mask(ltx_text):
        raw_ltx = ltx_text.group(0)
        label = f'(L{len(labels) + 1:04})'
        labels[label] = raw_ltx
        return label

    text = re.sub(r'\$([^\$]+)\$', _make_mask, text)
    return text

def unmask(labels, text):
    for mask, raw in labels.items():
        text = text.replace(mask, raw)
    return text

def get_channel_id(channel_names):
    client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    conv_list = client.conversations_list()["channels"]
    channel_dict = {}
    for channel in conv_list:
        if channel['name'] in channel_names:
            channel_dict[channel['name']] = channel['id']
    return channel_dict


def get_user_id(usernames: dict):
    client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    user_dict = client.users_list()["members"]
    user_id_dict = {}
    for user in user_dict:
        if user['real_name'] in usernames:
            user_id_dict[user['real_name']] = user['id']
    return user_id_dict


def delete_history_message(slack_channel: str) -> None:
    client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    days = 31
    storage_term = 60 * 60 * 24 * days  # 1ヶ月
    now = datetime.datetime.now()
    current_ts = int(now.strftime('%s'))
    # Store conversation history
    try:
        # get history
        endline = now - datetime.timedelta(days=days)
        endline_ts = endline.strftime('%s') + '.000000'
        result = client.conversations_history(
            channel=slack_channel,
            latest=endline_ts,
            limit=100
        )
        conversation_history = result["messages"]
        # delete
        for message in conversation_history:
            if 'bot_id' in message:
                if message['bot_id']==os.getenv('SLACK_BOT_ID'):
                    if current_ts - int(re.sub(r'\.\d+$', '', message['ts'])) > storage_term:
                        del_result = client.chat_delete(
                            channel=slack_channel,
                            ts=message['ts']
                        )
                        logger.info(del_result)
                        time.sleep(2)
    except SlackApiError as e:
        # You will get a SlackApiError if "ok" is False
        print('ERROR!')
        print(e)
        assert e.response["error"]    # str like 'invalid_auth', 'channel_not_found'
    

def get_mention(title: str, abstract: str, mention_dict: dict, user_id_dict: dict, channel_name: str) -> str:
    mention = ''
    mention_list = []
    content = title + ' ' + abstract 
    content = content.lower()
    for name in mention_dict:
        if name not in user_id_dict:
            print('Someone assign wrong name in xlsx file! Please confirm.')
        keywords = mention_dict[name][channel_name].dropna().values.tolist()
        for keyword in keywords:
            if keyword.lower() in content:
                mention_list.append('<@'+user_id_dict[name]+'>')
                break
    if len(mention_list) > 0:
        mention = '\n' + ' '.join(mention_list)
    return mention


def send2app(text: str, slack_channel: str, line_token: str) -> None:
    if slack_channel is not None:
        client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

        try:
            response = client.chat_postMessage(
                channel=slack_channel,
                text=text,
                unfurl_links=False,
                mrkdwn=False
            ) 
        except SlackApiError as e:
            # You will get a SlackApiError if "ok" is False
            print('ERROR!')
            print(e)
            assert e.response["error"]    # str like 'invalid_auth', 'channel_not_found'
    
    # line
    if line_token is not None:
        line_notify_api = 'https://notify-api.line.me/api/notify'
        headers = {'Authorization': f'Bearer {line_token}'}
        data = {'message': f'message: {text}'}
        requests.post(line_notify_api, headers=headers, data=data)


def notify(results: list, slack_channel: str, line_token: str, mention_dict: dict, user_id_dict: dict, channel_name: str) -> None:
    # 通知
    star = '*'*80
    
    deadline_str, previous_deadline_str = get_date_range(style='%Y/%m/%d %H:%M:%S')
    day_range = f'{previous_deadline_str} 〜 {deadline_str} UTC'
    
    n_articles = len(results)
    text = f'{star}\n \t \t {day_range}\tnum of articles = {n_articles}\n{star}'
    send2app(text, slack_channel, line_token)
    # descending
    for result in sorted(results, reverse=True, key=lambda x: x.score):
        url = result.url
        title = result.title
        en_title = result.en_title
        abstract = result.abstract
        en_abstract = result.en_abstract
        word = result.words
        score = result.score
        
        title = title.replace('$', ' ')
        abstract = abstract.replace('$', ' ')
        en_abstract = re.sub(r' *([_\*~]) *', r'\1', en_abstract)
        en_abstract = en_abstract.replace("`", "'")
        abstract = re.sub(r' *([_\*~]) *', r'\1', abstract)
        abstract = abstract.replace("`", "'")
#         abstract = '```\t' + abstract + '```'
        mention = get_mention(en_title, en_abstract, mention_dict, user_id_dict, channel_name)

        text = f'\n Title:\t{title}'\
               f'\n English Title:\t{en_title}'\
               f'\n URL: {url}'\
               f'{mention}'\
               f'\n Abstract:'\
               f'\n {abstract}'\
               f'\n English abstract:'\
               f'\n \t {en_abstract}'\
               f'\n {star}'

        send2app(text, slack_channel, line_token)


def get_translated_text(from_lang: str, to_lang: str, from_text: str, driver) -> str:
    '''
    https://qiita.com/fujino-fpu/items/e94d4ff9e7a5784b2987
    '''

    sleep_time = 1
    
    # mask latex mathline
    labels = {}
    print(repr(from_text))
    from_text = mask(labels, from_text)

    # urlencode
    from_text = urllib.parse.quote(from_text, safe='')
    from_text = from_text.replace('%2F', '%5C%2F')
    

    # url作成
    url = 'https://www.deepl.com/translator#' \
        + from_lang + '/' + to_lang + '/' + from_text

    driver.get(url)
    driver.implicitly_wait(10)  # 見つからないときは、10秒まで待つ

    for i in range(50):
        # 指定時間待つ
        time.sleep(sleep_time)
        html = driver.page_source
        to_text = get_text_from_page_source(html)

        if to_text:
            break
    if to_text is None:
        to_text = 'Sorry, I timed out...>_<'
    print(to_text)
    
    # unmask latex mathline
    to_text = to_text.replace('（', '(').replace('）', ')')  # to prevent from change label by deepL
    to_text = unmask(labels, to_text)

    return to_text


def get_text_from_page_source(html: str) -> str:
    soup = BeautifulSoup(html, features='lxml')
    print(soup)
    target_elem = soup.find(class_="lmt__translations_as_text__text_btn")
    text = target_elem.text
    text = ' '.join(text.split())
    return text


def get_config() -> dict:
    file_abs_path = os.path.abspath(__file__)
    file_dir = os.path.dirname(file_abs_path)
    config_path = f'{file_dir}/../config.yaml'
    with open(config_path, 'r') as yml:
        config = yaml.load(yml)
    return config


def get_previous_deadline(day):
    deadline = day - datetime.timedelta(days=1)
    previous_deadline = deadline - datetime.timedelta(days=1)
    if day.weekday()==0:  # announce data is Monday
        deadline = deadline - datetime.timedelta(days=2)
        previous_deadline = previous_deadline - datetime.timedelta(days=2)
    if day.weekday()==1:  # announce data is Tuesday
        previous_deadline = previous_deadline - datetime.timedelta(days=2)
    return deadline, previous_deadline


def read_holidayfile():
    path = 'arxiv_holiday.yaml'
    with open(path) as file:
        obj = yaml.safe_load(file)
    holiday = [datetime.datetime.strptime(date, '%Y/%m/%d').date()  for date in obj['holiday']]
    announce_holiday = [date + datetime.timedelta(days=1) for date in holiday]
    return announce_holiday


def get_date_range(style='%Y%m%d%H%M%S'):
    # us_holidays = holidays.US()
    us_holidays = read_holidayfile()
    day = datetime.datetime.today()
    deadline, previous_deadline = get_previous_deadline(day)
    # check holiday
    print(day, us_holidays)
    if day.date() in us_holidays:
        print('It is a holiday today!!! (^_^)')
        exit()
    while True:
        # cal previous day
        if day.weekday()==0:
            delta = datetime.timedelta(days=3)
        else:
            delta = datetime.timedelta(days=1)
        day = day - delta
        if day.date() not in us_holidays:
            break
        # extend previous_deadline
        _, previous_deadline = get_previous_deadline(day)
    
    deadline = deadline.replace(hour=19, minute=0, second=0, microsecond=0)
    previous_deadline = previous_deadline.replace(hour=19, minute=0, second=0, microsecond=0)
    tz = pytz.timezone('US/Eastern')
    deadline = deadline - tz.dst(deadline)
    previous_deadline = previous_deadline - tz.dst(previous_deadline)
    deadline_str = deadline.strftime(style)
    previous_deadline_str = previous_deadline.strftime(style)
    return deadline_str, previous_deadline_str


def main():
    # debug用
    parser = argparse.ArgumentParser()
    parser.add_argument('--slack_id', default=None)
    parser.add_argument('--line_token', default=None)
    args = parser.parse_args()

    config = get_config()
    channels = config['channels']
    score_threshold = float(config['score_threshold'])
    slack_channel_names = channels.keys()
    
#     # delete  
    channel_dict = get_channel_id(slack_channel_names)
    for channel_id in channel_dict.values():
        delete_history_message(channel_id)
    # # for debug
    # delete_history_message(os.getenv("SLACK_CHANNEL_ID_DEV"))
    
    # mention用データを読み込み
    mention_url = os.getenv("MENTION_URL")
    mention_dict = get_mention_dict(mention_url)
    user_id_dict = get_user_id(mention_dict.keys())

    # post
    deadline_str, previous_deadline_str = get_date_range()
    for channel_name, channel_config in channels.items():
        subject = channel_config['subject']
        keywords = channel_config['keywords']
        # datetime format YYYYMMDDHHMMSS
        arxiv_query = f'({subject}) AND ' \
                      f'submittedDate:' \
                      f'[{previous_deadline_str} TO {deadline_str}]'
        articles = arxiv.query(query=arxiv_query,
                               max_results=1000,
                               sort_by='submittedDate',
                               iterative=False)
        print(arxiv_query)
        results = search_keyword(articles, keywords, score_threshold)

        slack_id = channel_dict[channel_name]
        # slack_id = os.getenv("SLACK_CHANNEL_ID_DEV") or args.slack_id  # debug
        line_token = os.getenv("LINE_TOKEN") or args.line_token
        notify(results, slack_id, line_token, mention_dict, user_id_dict, channel_name)
        # break  # debug


if __name__ == "__main__":
    main()
