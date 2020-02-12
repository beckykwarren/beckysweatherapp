from datetime import datetime
import os
import pytz
import requests
from config import API_KEY
import math

# API_KEY = '802f4756bfe157d99d443305011036ed'
API_URL = ('http://api.openweathermap.org/data/2.5/weather?q={}&mode=json&units=metric&appid={}')


def query_api(city):    
    try:        
        print(API_URL.format(city, API_KEY))        
        data = requests.get(API_URL.format(city, API_KEY)).json()    
    except Exception as exc:        
        print(exc)        
        data = None    
    return data