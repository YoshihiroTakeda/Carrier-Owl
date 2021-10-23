import pandas as pd
import os
import requests

def get_mention_dict(url: str) ->dict:
    """get mention dict

    Args:
        url (str): the url of xlsx file. 
        Here, we assume a spreadsheet on Google Drive, and we receive a url of the following format:
        url = 'https://docs.google.com/spreadsheets/d/{key}/export?format=xlsx'

    Returns:
        dict: dict has usernames on slack as keys, and keywords DataFrame as values.
    """
    df_dict = pd.read_excel(url, sheet_name=None)
    return df_dict
    
if __name__ == "__main__":
    get_mention_dict(os.getenv('MENTION_URL'))