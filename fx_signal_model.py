import pandas as pd
import matplotlib.pyplot as plt
from ta import add_all_ta_features
from ta.utils import dropna
from ta.volatility import BollingerBands
import datetime
from datetime import timedelta
from datetime import date
import numpy as np
from tqdm import tqdm
import time
from time import sleep
import warnings
warnings.filterwarnings("ignore")
import random
from xbbg import blp
import schedule
import tkinter as tk
from tkinter import ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import win32com.client as win32

#%%
def obtain_fx_pair(fx_pair, period):
    #Identify today's datetime
    today = datetime.datetime.today()

    #Identify the dates of the past 14 trading days
    Date_range = []
    for i in range (period):
        date = datetime.datetime.today() - timedelta(days=i)
        date = date.strftime("%Y-%m-%d")
        Date_range.append(date)
    Date_range = Date_range[::-1] #invert the dates to a chronological order

    #Acquire USDCNH close data for the past 14 trading days
    Original_data = blp.bdib(ticker = fx_pair, dt=datetime.datetime.today()-timedelta(days=period), session="allday", typ="TRADE", interval = 60)["CNH Curncy"]["close"] #take the first day
    for date in Date_range: #take the next 13 days
        #print(date)
        a = blp.bdib(ticker = fx_pair, dt=date, session="allday", typ="TRADE", interval = 60)
        if not a.empty:
            data = a["CNH Curncy"]["close"]
            Original_data = pd.concat([Original_data, data])

    Original_data.index= Original_data.index.tz_convert(tz='Asia/Hong_Kong') #convert timezone
    Original_data = pd.DataFrame({'Date':Original_data.index, 'Close':Original_data.values}) #convert to dataframe
    Original_data["Date"] = Original_data["Date"]+timedelta(hours=1) #Adjust 1H to make data point in time

    return Original_data
#%%
def end_of_period_close_position(output_df, df):
# Close off the trade in the last day of the data set
    n_rows = output_df.shape[0]
    length = len(df["Date"])

    if output_df["Position"].iloc[n_rows-1] == 1: #if current position is long
        #sell at the last day's closing price
        temp = pd.DataFrame([["Today", df["Date"].iloc[length-1], df["Close"].iloc[length-1], 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
        output_df = pd.concat([output_df,temp])

    elif output_df["Position"].iloc[n_rows-1] == -1: #if current position is short
        #buy at the last day's closing price
        temp = pd.DataFrame([["Today", df["Date"].iloc[length-1], df["Close"].iloc[length-1], 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
        output_df = pd.concat([output_df,temp])

    return output_df
#%%
def calc_holding_period(output_df):
#Calculate holding period of each trade
    output_df["Holding Period"] = output_df["Position"]+1 #just to add another extra column

    n_rows = output_df.shape[0]
    for i in range(n_rows):
        if i == 0:
            output_df["Holding Period"].iloc[0] = output_df["Action Date"].iloc[0] - output_df["Action Date"].iloc[0] #minus itself to get a timedelta of zero

        else:
            if (output_df["Position"].iloc[i-1] == 1 or -1) and (output_df["Position"].iloc[i] == 0): #if we have an open position
                output_df["Holding Period"].iloc[i] = output_df["Action Date"].iloc[i] - output_df["Action Date"].iloc[i-1] #find the holding period

            else: #if we do not have an open position, set timedelta to 0
                output_df["Holding Period"].iloc[i] = output_df["Action Date"].iloc[i] - output_df["Action Date"].iloc[i] #minus itself to get a timedelta of zero

    output_df["Holding Period"] = output_df["Holding Period"]/pd.Timedelta(days=1)
    output_df["Holding Period"] = output_df["Holding Period"].astype(int)

    for i in range(output_df.shape[0]):
        if output_df["Holding Period"].iloc[i] == 0:
            output_df["Holding Period"].iloc[i] = ""

    return output_df
#%%
def calc_position_return(output_df, slippage):
#Calculate position return over the holding period of each trade
    output_df["Slippage Adj Return"] = output_df["Position"]+1 #just to add another extra column

    n_rows = output_df.shape[0]
    for i in range(n_rows):
        if i == 0: #if we are at the start date. no return because no position
            output_df["Slippage Adj Return"].iloc[0] = ""

        else:
            if output_df["Action Price"].iloc[i-1] == "": #this is equivalent to "if i = 1"
                output_df["Slippage Adj Return"].iloc[i] = ""

            else:
                if output_df["Position"].iloc[i-1] == 1: #if position = long
                    output_df["Slippage Adj Return"].iloc[i] = ((output_df["Action Price"].iloc[i]-slippage)/output_df["Action Price"].iloc[i-1])-1

                elif output_df["Position"].iloc[i-1] == -1: #if position = short
                    output_df["Slippage Adj Return"].iloc[i] = -(((output_df["Action Price"].iloc[i]+slippage)/output_df["Action Price"].iloc[i-1])-1)

                elif output_df["Position"].iloc[i-1] == 0: #if no position
                    output_df["Slippage Adj Return"].iloc[i] = ""

    return output_df
#%%
def cum_value(output_df):
    #Calculate cumulative value based on capital of 1
    output_df["Cumulative Value"] = output_df["Position"]+1 #just to add another extra column
    n_rows = output_df.shape[0]
    for j in range(n_rows):
        if j == 0: #if first row
            output_df["Cumulative Value"].iloc[j] = 1 #initialise cumulative value to 1

        else:
            if output_df["Slippage Adj Return"].iloc[j] == "": #if there is no return
                output_df["Cumulative Value"].iloc[j] = output_df["Cumulative Value"].iloc[j-1] #Today's cum value = yesterday's cum value

            else: #if there is return
                output_df["Cumulative Value"].iloc[j] = output_df["Cumulative Value"].iloc[j-1]*(1+(output_df["Slippage Adj Return"].iloc[j]))
                #increase today's cumulative value

    return output_df
#%%
def long_short_freq(output_df):
#Calculate long_short_frequency
    n_long = 0 #number of times we went long
    true_long = 0 #number of times we profit when we went long
    n_short = 0 #number of times we went short
    true_short = 0 #number of times we profit when we went short
    n_rows = output_df.shape[0]

    for i in range(n_rows):
        if output_df["Position"].iloc[i] == 1: #if position = long
            n_long = n_long+1 #then add 1 to number of times we went long
            if i < n_rows:
                if output_df["Slippage Adj Return"].iloc[i+1] > 0: #if return is positive
                    true_long = true_long+1 #we add 1 to true long

        elif output_df["Position"].iloc[i] == -1: #if position = short
            n_short = n_short+1 #then add 1 to number of times we went short
            if i < n_rows:
                if output_df["Slippage Adj Return"].iloc[i+1] > 0: #if return is positive
                    true_short = true_short+1 #we add 1 to true short

    return output_df, n_long, true_long, n_short, true_short
#%%
def sharpe_ratio(cum_ret, annual_SR):
#Calculate sharpe ratio
    daily_ret = pd.DataFrame(columns = ["Date","Excess Returns","Position", "Position Excess Ret"])
    cum_ret["Excess Returns"] = cum_ret["Returns"] - cum_ret["risk-free"] #This step is NOT redundant! We changed our returns in our circuit breaker function of the buy and sell signals section!

    for i in range(cum_ret.shape[0]):

        if i >= 1:
            if cum_ret["Position"].iloc[i-1] != 0: #if we have a position by the end of yesterday, today's return is taken into account
                daily_ret_nrows = daily_ret.shape[0]
                temp = pd.DataFrame([[cum_ret["Date"].iloc[i], cum_ret["Excess Returns"].iloc[i], cum_ret["Position"].iloc[i-1], cum_ret["Excess Returns"].iloc[i]*cum_ret["Position"].iloc[i-1]]], index = [daily_ret_nrows], columns = ["Date","Excess Returns","Position", "Position Excess Ret"])
                daily_ret = pd.concat([daily_ret, temp])

    annualized_mean_ex_ret = np.mean(daily_ret["Position Excess Ret"])*252*24
    annualized_stdev_ex_ret = np.std(daily_ret["Position Excess Ret"])*np.sqrt(252)*np.sqrt(24)
    annual_SR = round(annualized_mean_ex_ret/annualized_stdev_ex_ret,4)

    return annual_SR, annualized_mean_ex_ret
#%%
def calc_max_drawdown(Max_Drawdown,cum_ret): #Calcuate max drawdown
    window = cum_ret.shape[0]
    Roll_Max = cum_ret["Cumulative Value"].rolling(window, min_periods = 1).max()
    Drawdown = cum_ret["Cumulative Value"]/Roll_Max - 1.0
    Max_Drawdown = Drawdown.rolling(window, min_periods = 1).min()
    Perc_Drawdown = (Max_Drawdown/cum_ret["Cumulative Value"])*100
    Perc_Drawdown = Perc_Drawdown.sort_values()
    Max_Drawdown = Perc_Drawdown.iloc[0]

    return Max_Drawdown
#%%
def peak_price(cum_ret, daily_hl, i): #note the intraday peak corresponds to period up to i-1
    if i == 0: #if we are in day 1
        #if cum_ret["Position"].iloc[i] != 0: #if we have a position
            #cum_ret["Price at Peak"].iloc[i] = daily_hl["Close"].iloc[i] #today's close will be the price at peak

        #elif cum_ret["Position"].iloc[i] == 0: #if no position then today will have no peak price
        cum_ret["Price at Peak"].iloc[i] = 0 #peak price = 0

    else:
        if cum_ret["Position"].iloc[i-1] == 1: #if we have a long position
            if cum_ret["Price at Peak"].iloc[i-1] == 0: #today is the 1st day of the long position
                cum_ret["Price at Peak"].iloc[i] = daily_hl["Close"].iloc[i-1]

            else:
                if daily_hl["Close"].iloc[i-1] >= cum_ret["Price at Peak"].iloc[i-1]: #if yesterday's close >= the peak price before yesterday, replace
                    cum_ret["Price at Peak"].iloc[i] = daily_hl["Close"].iloc[i-1]

                elif daily_hl["Close"].iloc[i-1] < cum_ret["Price at Peak"].iloc[i-1]: #if yesterday's close < the peak price before yesterday, keep the price at peak
                    cum_ret["Price at Peak"].iloc[i] = cum_ret["Price at Peak"].iloc[i-1]

        elif cum_ret["Position"].iloc[i-1] == -1: #if we have a short position
            if cum_ret["Price at Peak"].iloc[i-1] == 0: #today is the 1st day of the short position
                cum_ret["Price at Peak"].iloc[i] = daily_hl["Close"].iloc[i-1]

            else: #if today is not the 1st day of the short position
                if daily_hl["Close"].iloc[i-1] <= cum_ret["Price at Peak"].iloc[i-1]: #if yesterday's close <= peak price before yesterday, replace
                    cum_ret["Price at Peak"].iloc[i] = daily_hl["Close"].iloc[i-1]

                elif daily_hl["Close"].iloc[i-1] > cum_ret["Price at Peak"].iloc[i-1]: #if yesterday's close > the peak price before yesterday, keep the price at peak
                    cum_ret["Price at Peak"].iloc[i] = cum_ret["Price at Peak"].iloc[i-1]

        elif cum_ret["Position"].iloc[i-1] == 0:
            cum_ret["Price at Peak"].iloc[i] = 0

    return cum_ret
#%%
def generate_signals (cum_ret, daily_hl, df, output_df, DD_limit, slippage):
#Generate Trading signals
# if the closing price of the previous trading day is below the buy zone and today's closing price is above the buy zone, go long
        #in long position, 3 possible cases
        #(1) Circuit breaker: close position if max drawdown > Drawdown limit
        #(2) Trailing stop loss, given by buy signal price - (take profit order price - buy signal price). This price is lower than the buy signal price
        #(3) the outer upper bollinger band is hit, take profit

# if the closing price of the previous trading day is above the sell zone and today's closing price is below the sell zone, go short
        #in short position, 3 possible cases
        #(1) Circuit breaker: close position if max drawdown > Drawdown limit
        #(2) Trailing stop loss, given by sell signal price - (take profit order price - buy signal price). This price is higher than the sell signal price
        #(3) the outer lower bollinger band is hit, take profit

    length = len(df["Date"]) #do not replace this with total_days! df was adjusted!
    for i in range(length):
        n_rows = output_df.shape[0] #find the number of rows of the output dataframe

        #if we are in the first day, initialise position if close price is above buy zone or below sell zone
        if i == 0: #If we do open a position, we assume we open position by the end of the day
            cum_ret = peak_price(cum_ret, daily_hl, i) #find the peak price before today's opening
            #buy if closing price is above the buy zone
            if (df["Close"].iloc[i] > df["Entry Price - Long"].iloc[i]):
                temp = pd.DataFrame([["Buy", df["Date"].iloc[i], round(df["Close"].iloc[i],4), 1]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                output_df = pd.concat([output_df,temp])
                cum_ret["Position"].iloc[i] = 1 #set status in the first day as "long"

            #sell if closing price is below the sell zone
            elif (df["Close"].iloc[i] < df["Entry Price - Short"].iloc[i]):
                temp = pd.DataFrame([["Sell", df["Date"].iloc[i], round(df["Close"].iloc[i],4), -1]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                output_df = pd.concat([output_df,temp])
                cum_ret["Position"].iloc[i] = -1 #set status in the first day as "short"

            else:
                cum_ret["Position"].iloc[i] = 0 #set status in the first day as "no position"

            cum_ret["Position Return"].iloc[i] = 0 #no position adjusted return since we do not have any position before the end of the day
            cum_ret["Cumulative Value"].iloc[i] = 1 #initialise starting capital = 1

        else: #if we are not in day 1
            cum_ret = peak_price(cum_ret, daily_hl, i) #find the peak price before today's opening
            #note that i-1 = yesterday and i = today
            #if we currently have a BUY position (last position is "1"):
            if output_df["Position"].iloc[n_rows-1] == 1:
                #Circuit Breaker - when Drawdown limit is exceeded, stop loss at stop loss price
                if daily_hl["Close"].iloc[i] < cum_ret["Price at Peak"].iloc[i]*(1-DD_limit):
                    stoploss_price = round(cum_ret["Price at Peak"].iloc[i]*(1-DD_limit),4)
                    temp = pd.DataFrame([["Sell", df["Date"].iloc[i], stoploss_price, 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"
                    cum_ret["Returns"].iloc[i] = ((stoploss_price-slippage)/daily_hl["Close"].iloc[i-1])-1 #change market return to (stop loss close price - slippage)/yesterday's close -1

                #trailing stop loss - sell at close price if trailing stop loss order is activated
                elif df["Close"].iloc[i] < (df["Entry Price - Long"].iloc[i-1] - (df["Exit Price - Long"].iloc[i-1] - df["Entry Price - Long"].iloc[i-1])):
                    #trailing stop loss - we allow ourselves to lose as much as the upside we want to earn. The upside is given by (take profit order price - buy signal price)
                    exit_price = (df["Entry Price - Long"].iloc[i-1] - (df["Exit Price - Long"].iloc[i-1] - df["Entry Price - Long"].iloc[i-1]))
                    temp = pd.DataFrame([["Sell", df["Date"].iloc[i], round(df["Close"].iloc[i],4), 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"
                    cum_ret["Returns"].iloc[i] = ((df["Close"].iloc[i]-slippage)/daily_hl["Close"].iloc[i-1])-1 #override market return to account for slippage

                #take profit order - if upper bollinger band is hit, close position at the close price
                elif df["Close"].iloc[i] > df["Exit Price - Long"].iloc[i]:
                    temp = pd.DataFrame([["Sell", df["Date"].iloc[i], round(df["Close"].iloc[i],4), 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"
                    cum_ret["Returns"].iloc[i] = ((df["Close"].iloc[i]-slippage)/daily_hl["Close"].iloc[i-1])-1 #change market return to (take profit close price - slippage)/yesterday's close -1

                else: #position is not closed, record position as long
                    cum_ret["Position"].iloc[i] = 1 #set status as "long"

                cum_ret["Position Return"].iloc[i] = cum_ret["Returns"].iloc[i] #long position so position adjusted return = market return
                cum_ret["Cumulative Value"].iloc[i] = cum_ret["Cumulative Value"].iloc[i-1]*(1+cum_ret["Position Return"].iloc[i]) #today's cumulative value = yesterday's*(1+position adjusted return)


            #if I currently have a SELL position:
            elif output_df["Position"].iloc[n_rows-1] == -1:
                cum_ret = peak_price(cum_ret, daily_hl, i) #find the peak price before today's opening
                #Circuit Breaker - when Drawdown limit is exceeded, stop loss at the stop loss price
                if daily_hl["Close"].iloc[i] > round(cum_ret["Price at Peak"].iloc[i]*(1+DD_limit),4):
                    stoploss_price = round(cum_ret["Price at Peak"].iloc[i]*(1+DD_limit),4)
                    temp = pd.DataFrame([["Buy", df["Date"].iloc[i], stoploss_price, 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"
                    cum_ret["Returns"].iloc[i] = ((stoploss_price + slippage)/daily_hl["Close"].iloc[i-1])-1 #change market return to (stop loss price + slippage)/yesterday's close -1

                #Trailing stop loss - if condition is triggered we sell at the of day price
                elif df["Close"].iloc[i] > (df["Entry Price - Short"].iloc[i-1] - (df["Exit Price - Short"].iloc[i-1] - df["Entry Price - Short"].iloc[i-1])):
                    #trailing stop loss - we allow ourselves to lose as much as the upside we want to earn. The upside is given by (take profit order price - buy signal price)
                    #exit_price = (df["Entry Price - Long"].iloc[i-1] - (df["Exit Price - Long"].iloc[i-1] - df["Entry Price - Long"].iloc[i-1]))
                    temp = pd.DataFrame([["Buy", df["Date"].iloc[i], round(df["Close"].iloc[i],4), 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"
                    cum_ret["Returns"].iloc[i] = ((df["Close"].iloc[i] + slippage)/daily_hl["Close"].iloc[i-1])-1 #change market return to stop loss price/yesterday's close -1

                elif df["Close"].iloc[i] < df["Exit Price - Short"].iloc[i]:
                    #if lower bollinger band is hit, close position
                    temp = pd.DataFrame([["Buy", df["Date"].iloc[i], round(df["Close"].iloc[i],4), 0]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"
                    cum_ret["Returns"].iloc[i] = ((df["Close"].iloc[i] + slippage)/daily_hl["Close"].iloc[i-1])-1 #change market return to (take profit close price + slippage)/yesterday's close -1

                else: #position is not closed, set position as short
                    cum_ret["Position"].iloc[i] = -1 #set status as "short"

                cum_ret["Position Return"].iloc[i] = -cum_ret["Returns"].iloc[i] #short position so position adjusted return = -1*market return
                cum_ret["Cumulative Value"].iloc[i] = cum_ret["Cumulative Value"].iloc[i-1]*(1+cum_ret["Position Return"].iloc[i]) #today's cumulative value = yesterday's*(1+position adjusted return)


            #if I currently have no position:
            elif output_df["Position"].iloc[n_rows-1] == 0:
                cum_ret = peak_price(cum_ret, daily_hl, i) #find the peak price before today's opening
                #buy at close price if yesterday's closing price is below the buy zone yesterday and today's closing price is above the buy zone today
                if (df["Close"].iloc[i-1] < df["Entry Price - Long"].iloc[i-1]) and (df["Close"].iloc[i] > df["Entry Price - Long"].iloc[i]):
                    temp = pd.DataFrame([["Buy", df["Date"].iloc[i], round(df["Close"].iloc[i],4), 1]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = 1 #set status as "long"
                    #cum_ret.iloc[i,1] = (daily_hl["Close"].iloc[i]/df.iloc[i,8])-1 #return from holding the position from the buy zone to the end of day price

                #sell at close price if yesterday's closing price is above the sell zone yesterday and today's closing price is below the sell zone today
                elif (df["Close"].iloc[i-1] > df["Entry Price - Short"].iloc[i-1]) and (df["Close"].iloc[i] < df["Entry Price - Short"].iloc[i]): #sell at the sell zone price
                    temp = pd.DataFrame([["Sell", df["Date"].iloc[i], round(df["Close"].iloc[i],4), -1]], index = [n_rows], columns = ["Action", "Action Date", "Action Price", "Position"])
                    output_df = pd.concat([output_df,temp])
                    cum_ret["Position"].iloc[i] = -1 #set status as "short"
                    #cum_ret.iloc[i,1] = (daily_hl["Close"].iloc[i]/df.iloc[i,9])-1 #return from holding the position from the sell zone to the end of day price

                else:
                    cum_ret["Position"].iloc[i] = 0 #set status as "no position"

                cum_ret["Position Return"].iloc[i] = 0
                cum_ret["Cumulative Value"].iloc[i] = cum_ret["Cumulative Value"].iloc[i-1]*(1+cum_ret["Position Return"].iloc[i])

    return output_df, cum_ret
#%% #In use, to obtain daily historical risk-free rate
def obtain_rf(rf_rate, period):
    today = datetime.datetime.today()
    st = today - timedelta(days=period)
    df = blp.bdh(rf_rate, "PX_LAST",st, today)
    return df
#%% #DISCONTINUED, to obtain daily historical data for fx pair
def obtain_data(ticker, end = datetime.datetime.today(), period = 90):
    st = end - timedelta(days=period)
    df = blp.bdh(ticker, ["PX_OPEN", "PX_HIGH","PX_LOW", "PX_LAST"],st, end)
    return df
#%% Function to run the Loop version and obtain parameters to be used for the day
def run_loop(SD_Adj, data_pts_low,data_pts_up, out_bb_sd_up, out_bb_sd_low, in_bb_sd_up, in_bb_sd_low, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14):

    Original_data = obtain_fx_pair(fx_pair, period)
    Original_data["Close1"] = Original_data["Close"]
    Original_data["Close2"] = Original_data["Close"]
    Original_data["Close3"] = Original_data["Close"]
    Original_data = Original_data.rename(columns={"Close": "Open", "Close1": "High", "Close2": "Low", "Close3": "Close"})
    Original_data = Original_data.iloc[:-1,:] #remove the last data from BBG which is less than 1 hour, our last data is until the 59th second of the 59th minute of the last hour

    #remove outlier data (all data point ends at xx02 but this one consistently ends at 0500.
    # Remove this or the return will be 2 min return for the next data point)
    for i in range(Original_data.shape[0]):
        if Original_data["Date"].loc[i].time() == datetime.time(5, 0):
            Original_data = Original_data.drop(int(i))
    Original_data = Original_data.reset_index().drop(columns="index") # Relabel Index

    #find return
    Close_s1 = Original_data["Close"].shift(1) #shift returns one day down
    Original_data["Returns"] = (Original_data["Close"]- pd.Series(Close_s1))/pd.Series(Close_s1) #find the daily returns


    #Obtain daily rf data
    rf = obtain_rf(rf_rate, period)[rf_rate]
    rf = rf.reset_index()
    rf = rf.rename(columns={"index": "Date", "PX_LAST": "risk-free"})
    rf["risk-free"] = rf["risk-free"].round(4)
    rf["risk-free"] = rf["risk-free"]/(100*365*24)

    #carry over yesterday's value to today for rf if there is no ef value today, because US time is behind HK time in the morning Bloomberg may not give us overnight risk-free rate data
    if rf["Date"].iloc[rf.shape[0]-1] < Original_data["Date"].iloc[Original_data.shape[0]-1].date():
        temp = pd.DataFrame([[Original_data["Date"].iloc[Original_data.shape[0]-1], rf["risk-free"].iloc[rf.shape[0]-1]]], index = [rf.shape[0]], columns = ["Date", "risk-free"])
        rf = pd.concat([rf,temp])

    #to create the returns dataframe with an unpopulated risk-free column
    returns = pd.concat([Original_data["Date"], Original_data["Returns"],Original_data["Close"]], axis = 1, join = "inner")
    returns = returns.rename(columns={"Date": "Date", "Returns": "Returns",  "Close": "risk-free"})

    #To populate the risk-free rate
    for i in range(returns.shape[0]):
        for j in range(rf.shape[0]):
            if returns["Date"].loc[i].date() == rf["Date"].loc[j].date():
                returns["risk-free"].loc[i] = rf["risk-free"].loc[j]

    #To overwrite the values that are not overwritten in the last for loop
    for i in range(returns.shape[0]):
        if returns["risk-free"].loc[i] > 0.5: #it is unlikely that USDCNH will fall below 0.5 and it is unlikely that risk-free rate per day can be greater than 0.5
            returns["risk-free"].loc[i] = returns["risk-free"].loc[i-1]

    #find excess return
    returns["Excess Returns"] = returns["Returns"] - returns["risk-free"]
    #create extra 4 columns
    returns["Position"] = returns["Excess Returns"] #Indicate position, 1 = long; 0 = no position; -1 = short
    returns["Position Return"] = returns["Position"] #Indicate position adjusted return
    returns["Cumulative Value"] = returns["Position"] #Indicate Cumulative value of $1
    returns["Price at Peak"] = returns["Position"] #Indicate the peak price of a position until the previous day (i.e., time i indicates the peak price until time i-1)

    #Create Dataframe for intraday high and low return to be used in each loop:
    Intraday_hl = pd.concat([Original_data["Date"], Original_data["High"], Original_data["Low"],Original_data["Close"]], axis = 1)

    #Summary statistics table output
    summary_stats = pd.DataFrame(columns = ["Data Points","Outer S.D.", "Inner S.D.", "Sharpe Ratio", "Calmar Ratio", "Period Return" , "Max Drawdown", "Long Signal", "Profit given Long", "Short Signal", "Profit given Short"])

    for p in tqdm(range(data_pts_low,data_pts_up+1,2)): #data points
        sleep(0.01)
        for q in range(out_bb_sd_low,out_bb_sd_up+1,10): #outer band
            q = q/100
            for r in range(in_bb_sd_low,in_bb_sd_up+1,5): #inner band
                r=r/100
                df = Original_data.copy()
                cum_ret = returns.copy()
                total_days = df.shape[0]

                #create bollinger bands
                df["SMA"] = df["Close"].rolling(window=p).mean() #Simple moving average
                df['Exit Price - Long'] = df["SMA"] + (q*SD_Adj*df["Close"].rolling(window=p).std())
                df['Exit Price - Short'] = df["SMA"] - (q*SD_Adj*df["Close"].rolling(window=p).std())
                df['Entry Price - Long'] = df["SMA"] + (r*SD_Adj*df["Close"].rolling(window=p).std())
                df['Entry Price - Short'] = df["SMA"] - (r*SD_Adj*df["Close"].rolling(window=p).std())

                #remove the upper bound of used data points from the data set
                df = df.tail(total_days - data_pts_up)
                cum_ret = cum_ret.tail(total_days - data_pts_up)
                daily_hl = Intraday_hl.tail(total_days - data_pts_up)

                #Create signal output dataframe for each loop
                #action = buy or sell, action date = date of buy or sell, action price = price of buy or sell, position: long = 1, 0 = no position, -1 = short
                output_df = pd.DataFrame(columns = ["Action","Action Date", "Action Price", "Position"])
                temp = pd.DataFrame([["Start date", df["Date"].iloc[0], "", 0]], index = [0], columns = ["Action", "Action Date", "Action Price", "Position"])
                output_df = pd.concat([output_df,temp])

                #Generate trading signals
                length = len(df["Date"])
                output_df, cum_ret = generate_signals (cum_ret, daily_hl, df, output_df, DD_limit, slippage)

                #Close off open positions at the end of the window
                output_df = end_of_period_close_position(output_df, df)

                #Calculate holding period of each trade
                output_df = calc_holding_period(output_df)

                #Calculate the holding period return of each trade
                output_df = calc_position_return(output_df, slippage)

                #Calculate cumulative value of 1
                output_df = cum_value(output_df)

                #Calculate long_short_frequency
                output_df, n_long, true_long, n_short, true_short = long_short_freq(output_df)

                #Calculate Sharpe ratio
                annual_SR = 0
                annual_SR, annualized_mean_ex_ret = sharpe_ratio(cum_ret, annual_SR)

                #Calcuate max drawdown
                Max_Drawdown = 0
                Max_Drawdown = calc_max_drawdown(Max_Drawdown,cum_ret)

                #Calmar ratio - calculated by multiplying daily mean average return by number of trading days in the trading window divided by the Max Drawdown in the period
                Calmar = -annualized_mean_ex_ret/(Max_Drawdown/100)/(252*24)*df.shape[0] #Max drawdown is in percentage, annualised_mean_ex_ret is in percentage

                #Calculate period return
                n_rows = output_df.shape[0]
                period_return = ((output_df["Cumulative Value"].iloc[n_rows-1]/output_df["Cumulative Value"].iloc[0])-1)*100 #round period return to 4 dp

                #If our position is still open then adjust the position status
                if output_df["Action"].iloc[-1] == "Today":
                    output_df["Position"].iloc[-1] = output_df["Position"].iloc[-2]

                #Update Summary Statistics
                temp = pd.DataFrame([[p, q, r, annual_SR, round(Calmar,4), round(period_return,4), round(Max_Drawdown,4), n_long, true_long, n_short, true_short]], index = [""], columns = ["Data Points","Outer S.D.", "Inner S.D.", "Sharpe Ratio", "Calmar Ratio", "Period Return", "Max Drawdown" , "Long Signal", "Profit given Long", "Short Signal", "Profit given Short"])
                summary_stats = pd.concat([summary_stats,temp])

    summary_stats_sorted = summary_stats.sort_values(by =["Period Return"], ascending = False)
    summary_stats_sorted = summary_stats_sorted.head(round(0.01*summary_stats_sorted.shape[0])) #keep the parameters that generate the top 1% return
    summary_stats_sorted = summary_stats_sorted.sort_values(by =["Sharpe Ratio"], ascending = False)
    top_result = summary_stats_sorted.head(5) #within the top 1% return, select the 5 with the highest sharpe ratio
    top_result = top_result.sort_values(by =["Period Return"], ascending = False)
    p = top_result["Data Points"].iloc[0]
    q = top_result["Outer S.D."].iloc[0]
    r = top_result["Inner S.D."].iloc[0]

    return p, q, r
#%% Single Loop that takes the p,q,r result and run once in the morning
def single_morning_run(p,q,r, SD_Adj, data_pts_up, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14):

    #Obtain intraday USDCNH data
    Original_data = obtain_fx_pair(fx_pair, period)
    Original_data["Close1"] = Original_data["Close"]
    Original_data["Close2"] = Original_data["Close"]
    Original_data["Close3"] = Original_data["Close"]
    Original_data = Original_data.rename(columns={"Close": "Open", "Close1": "High", "Close2": "Low", "Close3": "Close"})
    Original_data = Original_data.iloc[:-1,:] #remove the last data from BBG which is less than 1 hour, our last data is until the 59th second of the 59th minute of the last hour

    #remove outlier data (all data point ends at xx02 but this one consistently ends at 0500.
    # Remove this or the return will be 2 min return for the next data point)
    for i in range(Original_data.shape[0]):
        if Original_data["Date"].loc[i].time() == datetime.time(5, 0):
            Original_data = Original_data.drop(int(i))
    Original_data = Original_data.reset_index().drop(columns="index") # Relabel Index

    #find return
    Close_s1 = Original_data["Close"].shift(1) #shift returns one day down
    Original_data["Returns"] = (Original_data["Close"]- pd.Series(Close_s1))/pd.Series(Close_s1) #find the daily returns

    #Obtain daily rf data
    rf = obtain_rf(rf_rate, period)[rf_rate]
    rf = rf.reset_index()
    rf = rf.rename(columns={"index": "Date", "PX_LAST": "risk-free"})
    rf["risk-free"] = rf["risk-free"].round(4)
    rf["risk-free"] = rf["risk-free"]/(100*365*24)

    #carry over yesterday's value to today for rf if there is no ef value today, because US time is behind HK time in the morning Bloomberg may not give us overnight risk-free rate data
    if rf["Date"].iloc[rf.shape[0]-1] < Original_data["Date"].iloc[Original_data.shape[0]-1].date():
        temp = pd.DataFrame([[Original_data["Date"].iloc[Original_data.shape[0]-1], rf["risk-free"].iloc[rf.shape[0]-1]]], index = [rf.shape[0]], columns = ["Date", "risk-free"])
        rf = pd.concat([rf,temp])

    #to create the returns dataframe with an unpopulated risk-free column
    returns = pd.concat([Original_data["Date"], Original_data["Returns"],Original_data["Close"]], axis = 1, join = "inner")
    returns = returns.rename(columns={"Date": "Date", "Returns": "Returns",  "Close": "risk-free"})

    #To populate the risk-free rate
    for i in range(returns.shape[0]):
        for j in range(rf.shape[0]):
            if returns["Date"].loc[i].date() == rf["Date"].loc[j].date():
                returns["risk-free"].loc[i] = rf["risk-free"].loc[j]

    #To overwrite the values that are not overwritten in the last for loop
    for i in range(returns.shape[0]):
        if returns["risk-free"].loc[i] > 0.5: #it is unlikely that USDCNH will fall below 0.5 and it is unlikely that risk-free rate per hour can be greater than 0.5
            returns["risk-free"].loc[i] = returns["risk-free"].loc[i-1]

    #find excess return
    returns["Excess Returns"] = returns["Returns"] - returns["risk-free"]
    #create extra 4 columns
    returns["Position"] = returns["Excess Returns"] #Indicate position, 1 = long; 0 = no position; -1 = short
    returns["Position Return"] = returns["Position"] #Indicate position adjusted return
    returns["Cumulative Value"] = returns["Position"] #Indicate Cumulative value of $1
    returns["Price at Peak"] = returns["Position"] #Indicate the peak price of a position until the previous day (i.e., time i indicates the peak price until time i-1)

    #Create Dataframe for intraday high and low return to be used in each loop:
    Intraday_hl = pd.concat([Original_data["Date"], Original_data["High"], Original_data["Low"],Original_data["Close"]], axis = 1)

    df = Original_data.copy()
    cum_ret = returns.copy()
    total_days = df.shape[0]

    # Create bollinger bands
    #create bollinger bands
    df["SMA"] = df["Close"].rolling(window=p).mean() #Simple moving average
    df['Exit Price - Long'] = df["SMA"] + (q*SD_Adj*df["Close"].rolling(window=p).std())
    df['Exit Price - Short'] = df["SMA"] - (q*SD_Adj*df["Close"].rolling(window=p).std())
    df['Entry Price - Long'] = df["SMA"] + (r*SD_Adj*df["Close"].rolling(window=p).std())
    df['Entry Price - Short'] = df["SMA"] - (r*SD_Adj*df["Close"].rolling(window=p).std())

    #remove the upper bound of used data points from the data set
    df = df.tail(total_days - data_pts_up)
    cum_ret = cum_ret.tail(total_days - data_pts_up)
    daily_hl = Intraday_hl.tail(total_days - data_pts_up)

    #Create signal output dataframe for each loop
    #action = buy or sell, action date = date of buy or sell, action price = price of buy or sell, position: long = 1, 0 = no position, -1 = short
    output_df = pd.DataFrame(columns = ["Action","Action Date", "Action Price", "Position"])
    temp = pd.DataFrame([["Start date", df["Date"].iloc[0], "", 0]], index = [0], columns = ["Action", "Action Date", "Action Price", "Position"])
    output_df = pd.concat([output_df,temp])

    #Generate trading signals
    length = len(df["Date"])
    output_df, cum_ret = generate_signals (cum_ret, daily_hl, df, output_df, DD_limit, slippage)

    #Close off open positions at the end of the window
    output_df = end_of_period_close_position(output_df, df)

    #Calculate holding period of each trade
    output_df = calc_holding_period(output_df)

    #Calculate the holding period return of each trade
    output_df = calc_position_return(output_df, slippage)

    #Calculate cumulative value of 1
    output_df = cum_value(output_df)

    #Calculate long_short_frequency
    output_df, n_long, true_long, n_short, true_short = long_short_freq(output_df)

    #Calculate Sharpe ratio
    annual_SR = 0
    annual_SR, annualized_mean_ex_ret = sharpe_ratio(cum_ret, annual_SR)

    #Calcuate max drawdown
    Max_Drawdown = 0
    Max_Drawdown = calc_max_drawdown(Max_Drawdown,cum_ret)

    #Calmar ratio - calculated by multiplying daily mean average return by number of trading days in the trading window divided by the Max Drawdown in the period
    Calmar = -annualized_mean_ex_ret/(Max_Drawdown/100)/(252*24)*df.shape[0] #Max drawdown is in percentage, annualised_mean_ex_ret is in percentage

    #Calculate period return
    n_rows = output_df.shape[0]
    period_return = ((output_df["Cumulative Value"].iloc[n_rows-1]/output_df["Cumulative Value"].iloc[0])-1)*100 #round period return to 4 dp

    #If our position is still open then adjust the position status
    if output_df["Action"].iloc[-1] == "Today":
        output_df["Position"].iloc[-1] = output_df["Position"].iloc[-2]

    print("Data Points:",p,"; Outer SD:",q,"; Inner SD:",r)
    print("Start Date:", df["Date"].iloc[0])
    print("Holding period USDCNH return: ", round(((df["Close"].iloc[-1]/df["Close"].iloc[0])-1)*100,4),"%")
    print("Strategy Return during the period: ", round(period_return,4), "%")
    print("Annualised Sharpe Ratio: ", annual_SR)
    print("Max Drawdown: ", round(Max_Drawdown,4),"%")
    print("Calmar ratio:", round(Calmar,4))
    print("Number of times we went long: ", n_long)
    print("Number of times we profited given long: ", true_long)
    print("Number of times we went short: ", n_short)
    print("Number of times we profited given short: ", true_short)

    #Convert Date to a better format to read
    action_table = output_df.copy()
    action_table["Action Date"]=pd.to_datetime(action_table["Action Date"])
    action_table["Action Date"] = action_table["Action Date"].dt.strftime("%Y-%m-%d %H:%M")
    #print(action_table)

    n_rows = output_df.shape[0] #this can be remove depending on the position of this statement
    print("Hourly data, ","p: ", p,", q: ", q,", r: ", r, sep="")
    if output_df["Position"].iloc[n_rows-1] == 0:
        print("CNH Bol Band:")
        print("We currently do not have any position")
        print("Enter Buy Position at:", format(df["Entry Price - Long"].iloc[df.shape[0]-1],".4f"))
        print("Our Take Profit Price - Long is:", format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f"))
        print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f"))
        print("Our Max Drawdown Price:", format(df["Entry Price - Long"].iloc[df.shape[0]-1]*(1-DD_limit),".4f"))
        print()
        print("Enter Sell Position at:", format(df["Entry Price - Short"].iloc[df.shape[0]-1],".4f"))
        print("Our Take Profit Price - Short is:", format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f"))
        print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f"))
        print("Our Max Drawdown Price:", format(df["Entry Price - Short"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f"))

    elif output_df["Position"].iloc[n_rows-1] == 1: #if our position is long
        if (output_df["Action Date"].iloc[n_rows-2] == output_df["Action Date"].iloc[n_rows-1]) and (output_df["Position"].iloc[n_rows-2] == output_df["Position"].iloc[n_rows-1]): #if our current position is opened today
            print("CNH Bol Band:")
            print("Our current position is LONG")
            print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))
            print("Our take profit price is:", format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f"))
            print("Our Max Drawdown limit price is:", format(df["Close"].iloc[cum_ret.shape[0]-1]*(1-DD_limit),".4f"))
            print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f"))

        else:
            print("CNH Bol Band:")
            print("Our current position is LONG")
            print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))
            print("Our take profit price is:", format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f"))
            print("Our Max Drawdown limit price is:", format(cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1-DD_limit),".4f"))
            print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f"))

    elif output_df["Position"].iloc[n_rows-1] == -1: #if our position is short
        if (output_df["Action Date"].iloc[n_rows-2] == output_df["Action Date"].iloc[n_rows-1]) and (output_df["Position"].iloc[n_rows-2] == output_df["Position"].iloc[n_rows-1]): #if our current position is opened today
            print("CNH Bol Band:")
            print("Our current position is SHORT")
            print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))
            print("Our take profit price is:", format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f"))
            print("Our Max Drawdown limit price is:", format(df["Close"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f"))
            print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f"))

        else:
            print("CNH Bol Band:")
            print("Our current position is SHORT")
            print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))
            print("Our take profit price is:", format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f"))
            print("Our Max Drawdown limit price is:", format(cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f"))
            print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f"))

    #PLOT PRICE MOVEMENT
    plt.figure(figsize=(15,10))
    # line 1 points
    x1 = pd.Series(df["Date"])
    y1 = df['Close']
    x1 = x1.to_numpy()
    y1 = y1.to_numpy()
    # plotting the line 1 points
    plt.plot(x1, y1, label = "Close Price", linewidth = 3)

    # line 2 points
    x2 = pd.Series(df["Date"])
    y2 = df['Exit Price - Long']
    x2 = x2.to_numpy()
    y2 = y2.to_numpy()
    # plotting the line 2 points
    plt.plot(x2, y2, "y--", label = "Exit Price - Long")

    # line 3 points
    x3 = pd.Series(df["Date"])
    y3 = df['Exit Price - Short']
    x3 = x3.to_numpy()
    y3 = y3.to_numpy()
    # plotting the line 3 points
    plt.plot(x3, y3, "y--", label = "Exit Price - Short")

    # line 4 points
    x4 = pd.Series(df["Date"])
    y4 = df['Entry Price - Short']
    x4 = x4.to_numpy()
    y4 = y4.to_numpy()
    # plotting the line 4 points
    plt.plot(x4, y4, "r", label = "Entry Price - Short")

    # line 5 points
    x5 = pd.Series(df["Date"])
    y5 = df['Entry Price - Long']
    x5 = x5.to_numpy()
    y5 = y5.to_numpy()
    #plotting the line 4 points
    plt.plot(x5, y5, "g" , label = "Entry Price - Long")

    for i in range(output_df.shape[0]):
        if i == 1:
            if output_df["Position"].iloc[i] == 1: #if long
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1: #if short
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

        elif i > 1: #if we do not have a position in day 1
            if output_df["Position"].iloc[i] == 1 and output_df["Position"].iloc[i-1] == 0: #we opened a new long position after day 1
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1 and output_df["Position"].iloc[i-1] == 0: #we opened a new short position after day 1
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == 1: #if we close a long position today
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == -1: #if we close a short position today
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

    # naming the x axis
    plt.xlabel('x - axis')
    # naming the y axis
    plt.ylabel('y - axis')
    # giving a title to my graph
    plt.title('USDCNH momentum strategy')

    # show a legend on the plot
    plt.legend()

    # function to show the plot
    plt.show()

    return action_table, cum_ret
#%% Create a pop-up window to show that we opened a position
def open_position_signal_popup_window(output_df, df, cum_ret, DD_limit):
# Create the main window for the signal
    n_rows = output_df.shape[0] #this can be remove depending on the position of this statement
    print("Hourly data, ","p: ", p,", q: ", q,", r: ", r, sep="")

    root = tk.Tk()
    root.title("TRADE SIGNAL")
    root.geometry("900x700") #specify the size of the window

    #Create a text widget
    text = tk.Text(root, height = 8, width = 50)
    Font_tuple = ("Calibri", 12)
    text.configure(font = Font_tuple)
    text.pack()
    text.insert(tk.END, "p: " + format(p) + " , q: " + format(q) + " , r: " + format(r) + "\n")

    if output_df["Position"].iloc[n_rows-1] == 1: #if our position is long
        if (output_df["Action Date"].iloc[n_rows-2] == output_df["Action Date"].iloc[n_rows-1]) and (output_df["Position"].iloc[n_rows-2] == output_df["Position"].iloc[n_rows-1]): #To ensure our current position is opened NOW
            text.insert(tk.END, "CNH Bol Band:" + "\n")
            text.insert(tk.END, "We just entered a LONG position" + "\n")
            text.insert(tk.END, "Our Signal Price is:" + format(df["Entry Price - Long"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Entry Price is:" + format(df["Close"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Take Profit Price is:" + format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f") + "\n")
            text.insert(tk.END, "Our Max Drawdown Limit price is:" + format(df["Close"].iloc[cum_ret.shape[0]-1]*(1-DD_limit),".4f") + "\n")
            text.insert(tk.END, "Our Trailing Stop Loss Price is:" + format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f") + "\n")

    elif output_df["Position"].iloc[n_rows-1] == -1: #if our position is short
        if (output_df["Action Date"].iloc[n_rows-2] == output_df["Action Date"].iloc[n_rows-1]) and (output_df["Position"].iloc[n_rows-2] == output_df["Position"].iloc[n_rows-1]): #To ensure our current position is opened NOW
            text.insert(tk.END, "CNH Bol Band:" + "\n")
            text.insert(tk.END, "We just entered a SHORT position" + "\n")
            text.insert(tk.END, "Our Signal Price is:" + format(df["Entry Price - Short"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Entry Price is:" + format(df["Close"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Take Profit Price is:" + format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f") + "\n")
            text.insert(tk.END, "Our Max Drawdown Limit price is:" + format(df["Close"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f") + "\n")
            text.insert(tk.END, "Our Trailing Stop Loss Price is:" + format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f") + "\n")

    # Create a figure
    fig = Figure(figsize=(8, 5), dpi=100)
    ax = fig.add_subplot(111)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Close'].to_numpy(), label = "Close Price", linewidth = 3)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Long'].to_numpy(), "y--", label = "Exit Price - Long")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Short'].to_numpy(), "y--", label = "Exit Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Short'].to_numpy(), "r", label = "Entry Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Long'].to_numpy(), "g", label = "Entry Price - Long")

    for i in range(output_df.shape[0]):
        if i == 1:
            if output_df["Position"].iloc[i] == 1: #if long
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1: #if short
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

        elif i > 1: #if we do not have a position in day 1
            if output_df["Position"].iloc[i] == 1 and output_df["Position"].iloc[i-1] == 0: #we opened a new long position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1 and output_df["Position"].iloc[i-1] == 0: #we opened a new short position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == 1: #if we close a long position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == -1: #if we close a short position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

    # Create a canvas to embed the figure in the Tkinter window
    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.draw()
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH)
    #canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)

    def close_window():
        root.destroy()

    #Close button
    close_button = tk.Button(root, text = "Close", command = close_window)
    close_button.pack()

    #Close after 10 seconds (10000 milliseconds)
    root.after(30000, close_window)

    # Start the Tkinter event loop
    tk.mainloop()
# %% Open Position Signal
def open_position_signal(p, q, r, output_df, df, cum_ret, DD_limit):
    # Create the main window for the signal
    n_rows = output_df.shape[0] #this can be remove depending on the position of this statement

    cur_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-1])
    last_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-2])

    #Create an email object
    outlook = win32.Dispatch('outlook.application')
    mail = outlook.CreateItem(0)
    mail.To = "sx08829@citi.com; il39961@citi.com; ly25955@citi.com"
    #mail.Cc =

    #Create and save the plot
    fig = Figure(figsize=(15, 10), dpi=100)
    ax = fig.add_subplot(111)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Close'].to_numpy(), label = "Close Price", linewidth = 3)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Long'].to_numpy(), "y--", label = "Exit Price - Long")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Short'].to_numpy(), "y--", label = "Exit Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Short'].to_numpy(), "r", label = "Entry Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Long'].to_numpy(), "g", label = "Entry Price - Long")

    for i in range(output_df.shape[0]):
        if i == 1:
            if output_df["Position"].iloc[i] == 1: #if long
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1: #if short
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

        elif i > 1: #if we do not have a position in day 1
            if output_df["Position"].iloc[i] == 1 and output_df["Position"].iloc[i-1] == 0: #we opened a new long position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1 and output_df["Position"].iloc[i-1] == 0: #we opened a new short position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == 1: #if we close a long position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == -1: #if we close a short position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

    plot_path = 'plot.jpg'
    #fig.savefig("C:/Users/cg46381/OneDrive - Citi/FX Trading Rotation/Bol Band Model/" + plot_path, bbox_inches = "tight")
    fig.savefig("M:/HK_LM_Trading/Alex/" + plot_path, bbox_inches = "tight")


    # Attach the plot
    #mail.Attachments.Add("C:/Users/cg46381/OneDrive - Citi/FX Trading Rotation/Bol Band Model/" + plot_path)
    mail.Attachments.Add("M:/HK_LM_Trading/Alex/" + plot_path)

    #Add message
    #mail.Body = "Hello, Attached are the DataFrame and Matplotlib plot you requested."

    # Attach the action table
    html1 = output_df.to_html()

    if output_df["Position"].iloc[n_rows-1] == 1: #if our position is long
        if (output_df["Action Date"].iloc[n_rows-2] == output_df["Action Date"].iloc[n_rows-1]) and (output_df["Position"].iloc[n_rows-2] == output_df["Position"].iloc[n_rows-1]): #To ensure our current position is opened NOW

            #Print summary on interactive terminal
            print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
            print("We just entered a LONG position")
            print("Our Signal Price is:", format(df["Entry Price - Long"].iloc[df.shape[0]-1], ".4f"))
            print("Our Entry Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
            print("Our Take Profit Price is", format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f"))
            print("Our Max Drawdown Limit price is:", format(df["Close"].iloc[cum_ret.shape[0]-1]*(1-DD_limit),".4f"))
            print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f"))

            #Set Email Subject to "Open LONG Signal"
            mail.Subject = " OPEN LONG Signal "
            # HTML content for the DataFrame
            mail.HTMLBody = f"""
            <html>
            <head></head>
            <body>
            <p>Hello,</p>
            Hourly data, p: {p}, q: {q}, r: {r} <p>
            CNH Bol Band:<br>
            We just entered a LONG position <br>
            Our Signal Price is: {format(df["Entry Price - Long"].iloc[df.shape[0]-1], ".4f")} <br>
            Our Entry Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
            Our Take Profit Price is {format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f")} <br>
            Our Max Drawdown Limit price is: {format(df["Close"].iloc[cum_ret.shape[0]-1]*(1-DD_limit),".4f")} <br>
            Our Trailing Stop Loss Price is: {format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f")} <p>
            {html1}
            <p>Best,<br>Alex</p>
            </body>
            </html>
            """

    elif output_df["Position"].iloc[n_rows-1] == -1: #if our position is short
        if (output_df["Action Date"].iloc[n_rows-2] == output_df["Action Date"].iloc[n_rows-1]) and (output_df["Position"].iloc[n_rows-2] == output_df["Position"].iloc[n_rows-1]): #To ensure our current position is opened NOW

            print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
            print("We just entered a SHORT position")
            print("Our Signal Price is:", format(df["Entry Price - Short"].iloc[df.shape[0]-1], ".4f"))
            print("Our Entry Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
            print("Our Take Profit Price is", format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f"))
            print("Our Max Drawdown Limit price is:", format(df["Close"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f"))
            print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f"))

            #Set Email Subject to "Open SHORT Signal"
            mail.Subject = " OPEN SHORT Signal "
            # HTML content for the DataFrame
            mail.HTMLBody = f"""
            <html>
            <head></head>
            <body>
            <p>Hello,</p>
            Hourly data, p: {p}, q: {q}, r: {r} <p>
            CNH Bol Band:<br>
            We just entered a SHORT position <br>
            Our Signal Price is: {format(df["Entry Price - Short"].iloc[df.shape[0]-1], ".4f")} <br>
            Our Entry Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
            Our Take Profit Price is {format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f")} <br>
            Our Max Drawdown Limit price is: {format(df["Close"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f")} <br>
            Our Trailing Stop Loss Price is: {format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f")} <p>
            {html1}
            <p>Best,<br>Alex</p>
            </body>
            </html>
            """

    mail.Send()
#%% Close Position Signal
def close_position_signal(p, q, r, output_df, df, cum_ret, DD_limit):
    # Create the main window for the signal
    n_rows = output_df.shape[0] #this can be remove depending on the position of this statement

    cur_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-1])
    last_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-2])

    #Create an email object
    outlook = win32.Dispatch('outlook.application')
    mail = outlook.CreateItem(0)
    mail.To = "sx08829@citi.com; il39961@citi.com; ly25955@citi.com"
    #mail.Cc =

    #Create and save the plot
    fig = Figure(figsize=(15, 10), dpi=100)
    ax = fig.add_subplot(111)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Close'].to_numpy(), label = "Close Price", linewidth = 3)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Long'].to_numpy(), "y--", label = "Exit Price - Long")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Short'].to_numpy(), "y--", label = "Exit Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Short'].to_numpy(), "r", label = "Entry Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Long'].to_numpy(), "g", label = "Entry Price - Long")

    for i in range(output_df.shape[0]):
        if i == 1:
            if output_df["Position"].iloc[i] == 1: #if long
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1: #if short
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

        elif i > 1: #if we do not have a position in day 1
            if output_df["Position"].iloc[i] == 1 and output_df["Position"].iloc[i-1] == 0: #we opened a new long position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1 and output_df["Position"].iloc[i-1] == 0: #we opened a new short position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == 1: #if we close a long position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == -1: #if we close a short position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

    plot_path = 'plot.jpg'
    #fig.savefig("C:/Users/cg46381/OneDrive - Citi/FX Trading Rotation/Bol Band Model/" + plot_path, bbox_inches = "tight")
    fig.savefig("M:/HK_LM_Trading/Alex/" + plot_path, bbox_inches = "tight")

    # Attach the plot
    #mail.Attachments.Add("C:/Users/cg46381/OneDrive - Citi/FX Trading Rotation/Bol Band Model/" + plot_path)
    mail.Attachments.Add("M:/HK_LM_Trading/Alex/" + plot_path)

    # Attach the action table
    html1 = output_df.to_html()

    # if cum_ret["Position"].iloc[cum_ret.shape[0]-2] != 0 and cum_ret["Position"].iloc[cum_ret.shape[0]-1] == 0: #if we just closed our position now
    #     if cum_ret["Position"].iloc[cum_ret.shape[0]-2] == 1: #if we closed a LONG position

    #cur_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-1])
    #last_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-2])

    if last_position != 0 and cur_position == 0: #if we just closed our position now
        if last_position == 1: #if we closed a LONG position
            #Max Drawdown Stoploss order
            if df["Close"].iloc[df.shape[0]-1] < cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1-DD_limit): #the number of rows are the same for cum_ret and df
                #if our current price is lower than the max drawdown stop loss price, we use the max drawdown stop loss order
                exit_price = cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1-DD_limit)
                #print output on interactive terminal
                print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
                print("We close our LONG position on max drawdown stoploss order")
                print("Our Signal Max Drawdown Stop Loss Price is:", format(exit_price, ".4f"))
                print("Our Actual Exit Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
                print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))

                #Write email content
                mail.Subject = " Max Drawdown STOPLOSS on LONG Position "
                # HTML content for the DataFrame
                mail.HTMLBody = f"""
                <html>
                <head></head>
                <body>
                <p>Hello,</p>
                Hourly data, p: {p}, q: {q}, r: {r} <p>
                CNH Bol Band:<br>
                We close our LONG position on max drawdown stoploss order <br>
                Our Signal Max Drawdown Stop Loss Price is: {format(exit_price, ".4f")} <br>
                Our Actual Exit Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
                Our Entry Price was: {format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f")} <p>
                {html1}
                <p>Best,<br>Alex</p>
                </body>
                </html>
                """

            #Trailing Stoploss order
            elif df["Close"].iloc[df.shape[0]-1] < (df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])):
                #if our current price is lower than the trailing stop loss price, we use the trailing stop loss order
                exit_price = (df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1]))

                #print output on interactive terminal
                print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
                print("We close our LONG position on trailing stop loss order")
                print("Our Signal Trailing Stop Loss Price is:", format(exit_price, ".4f"))
                print("Our Actual Exit Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
                print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))

                #Write email content
                mail.Subject = " Trailing STOPLOSS on LONG Position "
                # HTML content for the DataFrame
                mail.HTMLBody = f"""
                <html>
                <head></head>
                <body>
                <p>Hello,</p>
                Hourly data, p: {p}, q: {q}, r: {r} <p>
                CNH Bol Band:<br>
                We close our LONG position on trailing stop loss order <br>
                Our Signal Trailing Stop Loss Price is: {format(exit_price, ".4f")} <br>
                Our Actual Exit Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
                Our Entry Price was: {format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f")} <p>
                {html1}
                <p>Best,<br>Alex</p>
                </body>
                </html>
                """

            # Take Profit Order
            elif df["Close"].iloc[df.shape[0]-1] > df["Exit Price - Long"].iloc[df.shape[0]-1]:
                #take profit if price > take profit price
                exit_price = df["Exit Price - Long"].iloc[df.shape[0]-1]

                #print output on interactive terminal
                print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
                print("We take profit on our LONG position")
                print("Our Signal Take Profit Price is:", format(exit_price, ".4f"))
                print("Our Actual Take Profit Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
                print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))

                #Write email content
                mail.Subject = " Take Profit on LONG Position "
                # HTML content for the DataFrame
                mail.HTMLBody = f"""
                <html>
                <head></head>
                <body>
                <p>Hello,</p>
                Hourly data, p: {p}, q: {q}, r: {r} <p>
                CNH Bol Band:<br>
                We take profit on our LONG position <br>
                Our Signal Take Profit Price is: {format(exit_price, ".4f")} <br>
                Our Actual Take Profit Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
                Our Entry Price was: {format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f")} <p>
                {html1}
                <p>Best,<br>Alex</p>
                </body>
                </html>
                """

        elif last_position == -1: #if we closed a SHORT position
            #Max Drawdown Stoploss order
            if df["Close"].iloc[df.shape[0]-1] > cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1+DD_limit): #the number of rows are the same for cum_ret and df
                #if our current price is lower than the max drawdown stop loss price, we use the max drawdown stop loss order
                exit_price = cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1+DD_limit)

                #print output on interactive terminal
                print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
                print("We close our SHORT position on max drawdown stoploss order")
                print("Our Signal Max Drawdown Stop Loss Price is:", format(exit_price, ".4f"))
                print("Our Actual Exit Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
                print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))

                #Write email content
                mail.Subject = " Max Drawdown STOPLOSS on SHORT Position "
                # HTML content for the DataFrame
                mail.HTMLBody = f"""
                <html>
                <head></head>
                <body>
                <p>Hello,</p>
                Hourly data, p: {p}, q: {q}, r: {r} <p>
                CNH Bol Band:<br>
                We close our SHORT position on max drawdown stoploss order <br>
                Our Signal Max Drawdown Stop Loss Price is: {format(exit_price, ".4f")} <br>
                Our Actual Exit Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
                Our Entry Price was: {format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f")} <p>
                {html1}
                <p>Best,<br>Alex</p>
                </body>
                </html>
                """

            #Trailing Stoploss order
            elif df["Close"].iloc[df.shape[0]-1] > (df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])):
                #if our current price is lower than the trailing stop loss price, we use the trailing stop loss order
                exit_price = (df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1]))

                #print output on interactive terminal
                print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
                print("We close our SHORT position on trailing stop loss order")
                print("Our Signal Trailing Stop Loss Price is:", format(exit_price, ".4f"))
                print("Our Actual Exit Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
                print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))

                #Write email content
                mail.Subject = " Trailing STOPLOSS on SHORT Position "
                # HTML content for the DataFrame
                mail.HTMLBody = f"""
                <html>
                <head></head>
                <body>
                <p>Hello,</p>
                Hourly data, p: {p}, q: {q}, r: {r} <p>
                CNH Bol Band:<br>
                We close our SHORT position on trailing stop loss order <br>
                Our Signal Trailing Stop Loss Price is: {format(exit_price, ".4f")} <br>
                Our Actual Exit Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
                Our Entry Price was: {format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f")} <p>
                {html1}
                <p>Best,<br>Alex</p>
                </body>
                </html>
                """

            # Take Profit Order
            elif df["Close"].iloc[df.shape[0]-1] < df["Exit Price - Short"].iloc[df.shape[0]-1]:
                #take profit if price < take profit price
                exit_price = df["Exit Price - Short"].iloc[df.shape[0]-1]

                #print output on interactive terminal
                print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
                print("We take profit on our SHORT position")
                print("Our Signal Take Profit Price is:", format(exit_price, ".4f"))
                print("Our Actual Take Profit Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f"))
                print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))

                #Write email content
                mail.Subject = " Take Profit on SHORT Position "
                # HTML content for the DataFrame
                mail.HTMLBody = f"""
                <html>
                <head></head>
                <body>
                <p>Hello,</p>
                Hourly data, p: {p}, q: {q}, r: {r} <p>
                CNH Bol Band:<br>
                We take profit on our SHORT position <br>
                Our Signal Take Profit Price is: {format(exit_price, ".4f")} <br>
                Our Actual Take Profit Price is: {format(df["Close"].iloc[df.shape[0]-1], ".4f")} <br>
                Our Entry Price was: {format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f")} <p>
                {html1}
                <p>Best,<br>Alex</p>
                </body>
                </html>
                """

    mail.Send()
#%% Create a pop-up window to show that we opened a position
def close_position_signal_popup_window(output_df, df, cum_ret, DD_limit):
# Create the main window for the signal
    n_rows = output_df.shape[0] #this can be remove depending on the position of this statement
    print("Hourly data, ","p: ", p,", q: ", q,", r: ", r, sep="")

    root = tk.Tk()
    root.title("TRADE SIGNAL")
    root.geometry("900x700") #specify the size of the window

    #Create a text widget
    text = tk.Text(root, height = 8, width = 50)
    Font_tuple = ("Calibri", 12)
    text.configure(font = Font_tuple)
    text.pack()
    text.insert(tk.END, "p: " + format(p) + " , q: " + format(q) + " , r: " + format(r) + "\n")

    if cum_ret["Position"].iloc[cum_ret.shape[0]-2] != 0 and cum_ret["Position"].iloc[cum_ret.shape[0]-1] == 0: #if we just closed our position now
        if cum_ret["Position"].iloc[cum_ret.shape[0]-2] == 1: #if we closed a LONG position
            text.insert(tk.END, "CNH Bol Band:" + "\n")
            text.insert(tk.END, "We just close our LONG" + "\n")
            text.insert(tk.END, "Our Signal TP Price is:", format(df["Exit Price - Long"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Actual TP Price is:", format(df["Close"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Entry Price was:" + format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f") + "\n")

        elif cum_ret["Position"].iloc[cum_ret.shape[0]-2] == -1: #if we closed a SHORT position
            text.insert(tk.END, "CNH Bol Band:" + "\n")
            text.insert(tk.END, "We just close our SHORT" + "\n")
            text.insert(tk.END, "Our Signal TP Price is:" + format(df["Exit Price - Short"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Actual TP Price is:" + format(df["Close"].iloc[df.shape[0]-1], ".4f") + "\n")
            text.insert(tk.END, "Our Entry Price was:" + format(output_df["Action Price"].iloc[output_df.shape[0]-2] + ".4f"))

    # Create a figure
    fig = Figure(figsize=(8, 5), dpi=100)
    ax = fig.add_subplot(111)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Close'].to_numpy(), label = "Close Price", linewidth = 3)
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Long'].to_numpy(), "y--", label = "Exit Price - Long")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Short'].to_numpy(), "y--", label = "Exit Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Short'].to_numpy(), "r", label = "Entry Price - Short")
    ax.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Long'].to_numpy(), "g", label = "Entry Price - Long")

    for i in range(output_df.shape[0]):
        if i == 1:
            if output_df["Position"].iloc[i] == 1: #if long
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1: #if short
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

        elif i > 1: #if we do not have a position in day 1
            if output_df["Position"].iloc[i] == 1 and output_df["Position"].iloc[i-1] == 0: #we opened a new long position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1 and output_df["Position"].iloc[i-1] == 0: #we opened a new short position after day 1
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == 1: #if we close a long position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == -1: #if we close a short position today
                ax.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

    # Create a canvas to embed the figure in the Tkinter window
    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.draw()
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH)

    def close_window():
        root.destroy()

    #Close button
    close_button = tk.Button(root, text = "Close", command = close_window)
    close_button.pack()

    #Close after 10 seconds (10000 milliseconds)
    root.after(30000, close_window)

    # Start the Tkinter event loop
    tk.mainloop()
#%%
def no_signal(p, q, r, last_position, cur_position, output_df, df, cum_ret):
    if last_position == 0 and cur_position == 0: #if we do not have any position
        print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
        print("We currently do not have any position")
        print("Enter Buy Position at:", format(df["Entry Price - Long"].iloc[df.shape[0]-1],".4f"))
        print("Our Take Profit Price - Long is:", format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f"))
        print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f"))
        print("Our Max Drawdown Price:", format(df["Entry Price - Long"].iloc[df.shape[0]-1]*(1-DD_limit),".4f"))
        print()
        print("Enter Sell Position at:", format(df["Entry Price - Short"].iloc[df.shape[0]-1],".4f"))
        print("Our Take Profit Price - Short is:", format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f"))
        print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f"))
        print("Our Max Drawdown Price:", format(df["Entry Price - Short"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f"))

    elif last_position == 1 and cur_position == 1: #if we are IN a long position
        print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
        print("Our current position is LONG")
        print("Our entry price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2],".4f"))
        print("Our take profit price is:", format(df["Exit Price - Long"].iloc[df.shape[0]-1],".4f"))
        print("Our Max Drawdown limit price is:", format(cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1-DD_limit),".4f"))
        print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Long"].iloc[df.shape[0]-1] - (df["Exit Price - Long"].iloc[df.shape[0]-1] - df["Entry Price - Long"].iloc[df.shape[0]-1])),".4f"))

    elif last_position == -1 and cur_position == -1: #if we are IN a short position
        print("CNH Bol Band; ","p: ", p,", q: ", q,", r: ", r, sep="")
        print("Our current position is SHORT")
        print("Our Entry Price was:", format(output_df["Action Price"].iloc[output_df.shape[0]-2], ".4f"))
        print("Our take profit price is:", format(df["Exit Price - Short"].iloc[df.shape[0]-1],".4f"))
        print("Our Max Drawdown limit price is:", format(cum_ret["Price at Peak"].iloc[cum_ret.shape[0]-1]*(1+DD_limit),".4f"))
        print("Our Trailing Stop Loss Price is:", format((df["Entry Price - Short"].iloc[df.shape[0]-1] - (df["Exit Price - Short"].iloc[df.shape[0]-1] - df["Entry Price - Short"].iloc[df.shape[0]-1])),".4f"))
#%% Single recurring run to be run every hour to generate signals
def single_recurring_run(p,q,r, SD_Adj, data_pts_up, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14):
    time.sleep(15) #wait 10 sec for BBG to update its database
    print("We start running at", datetime.datetime.now())

    #Obtain intraday USDCNH data
    Original_data = obtain_fx_pair(fx_pair, period)
    Original_data["Close1"] = Original_data["Close"]
    Original_data["Close2"] = Original_data["Close"]
    Original_data["Close3"] = Original_data["Close"]
    Original_data = Original_data.rename(columns={"Close": "Open", "Close1": "High", "Close2": "Low", "Close3": "Close"})
    Original_data = Original_data.iloc[:-1,:] #remove the last data from BBG which is less than 1 hour, our last data is until the 59th second of the 59th minute of the last hour

    #remove outlier data (all data point ends at xx02 but this one consistently ends at 0500.
    # Remove this or the return will be 2 min return for the next data point)
    for i in range(Original_data.shape[0]):
        if Original_data["Date"].loc[i].time() == datetime.time(5, 0):
            Original_data = Original_data.drop(int(i))
    Original_data = Original_data.reset_index().drop(columns="index") # Relabel Index

    #find return
    Close_s1 = Original_data["Close"].shift(1) #shift returns one day down
    Original_data["Returns"] = (Original_data["Close"]- pd.Series(Close_s1))/pd.Series(Close_s1) #find the daily returns

    #Obtain daily rf data
    rf = obtain_rf(rf_rate, period)[rf_rate]
    rf = rf.reset_index()
    rf = rf.rename(columns={"index": "Date", "PX_LAST": "risk-free"})
    rf["risk-free"] = rf["risk-free"].round(4)
    rf["risk-free"] = rf["risk-free"]/(100*365*24)

    #carry over yesterday's value to today for rf if there is no ef value today, because US time is behind HK time in the morning Bloomberg may not give us overnight risk-free rate data
    if rf["Date"].iloc[rf.shape[0]-1] < Original_data["Date"].iloc[Original_data.shape[0]-1].date():
        temp = pd.DataFrame([[Original_data["Date"].iloc[Original_data.shape[0]-1], rf["risk-free"].iloc[rf.shape[0]-1]]], index = [rf.shape[0]], columns = ["Date", "risk-free"])
        rf = pd.concat([rf,temp])

    #to create the returns dataframe with an unpopulated risk-free column
    returns = pd.concat([Original_data["Date"], Original_data["Returns"],Original_data["Close"]], axis = 1, join = "inner")
    returns = returns.rename(columns={"Date": "Date", "Returns": "Returns",  "Close": "risk-free"})

    #To populate the risk-free rate
    for i in range(returns.shape[0]):
        for j in range(rf.shape[0]):
            if returns["Date"].loc[i].date() == rf["Date"].loc[j].date():
                returns["risk-free"].loc[i] = rf["risk-free"].loc[j]

    #To overwrite the values that are not overwritten in the last for loop
    for i in range(returns.shape[0]):
        if returns["risk-free"].loc[i] > 0.5: #it is unlikely that USDCNH will fall below 0.5 and it is unlikely that risk-free rate per hour can be greater than 0.5
            returns["risk-free"].loc[i] = returns["risk-free"].loc[i-1]

    #find excess return
    returns["Excess Returns"] = returns["Returns"] - returns["risk-free"]
    #create extra 4 columns
    returns["Position"] = returns["Excess Returns"] #Indicate position, 1 = long; 0 = no position; -1 = short
    returns["Position Return"] = returns["Position"] #Indicate position adjusted return
    returns["Cumulative Value"] = returns["Position"] #Indicate Cumulative value of $1
    returns["Price at Peak"] = returns["Position"] #Indicate the peak price of a position until the previous day (i.e., time i indicates the peak price until time i-1)

    #Create Dataframe for intraday high and low return to be used in each loop:
    Intraday_hl = pd.concat([Original_data["Date"], Original_data["High"], Original_data["Low"],Original_data["Close"]], axis = 1)

    df = Original_data.copy()
    cum_ret = returns.copy()
    total_days = df.shape[0]

    # Create bollinger bands
    #create bollinger bands
    df["SMA"] = df["Close"].rolling(window=p).mean() #Simple moving average
    df['Exit Price - Long'] = df["SMA"] + (q*SD_Adj*df["Close"].rolling(window=p).std())
    df['Exit Price - Short'] = df["SMA"] - (q*SD_Adj*df["Close"].rolling(window=p).std())
    df['Entry Price - Long'] = df["SMA"] + (r*SD_Adj*df["Close"].rolling(window=p).std())
    df['Entry Price - Short'] = df["SMA"] - (r*SD_Adj*df["Close"].rolling(window=p).std())

    #remove the upper bound of used data points from the data set
    df = df.tail(total_days - data_pts_up)
    cum_ret = cum_ret.tail(total_days - data_pts_up)
    daily_hl = Intraday_hl.tail(total_days - data_pts_up)

    #Create signal output dataframe for each loop
    #action = buy or sell, action date = date of buy or sell, action price = price of buy or sell, position: long = 1, 0 = no position, -1 = short
    output_df = pd.DataFrame(columns = ["Action","Action Date", "Action Price", "Position"])
    temp = pd.DataFrame([["Start date", df["Date"].iloc[0], "", 0]], index = [0], columns = ["Action", "Action Date", "Action Price", "Position"])
    output_df = pd.concat([output_df,temp])

    #Generate trading signals
    length = len(df["Date"])
    output_df, cum_ret = generate_signals (cum_ret, daily_hl, df, output_df, DD_limit, slippage)

    #Close off open positions at the end of the window
    output_df = end_of_period_close_position(output_df, df)

    #Calculate holding period of each trade
    output_df = calc_holding_period(output_df)

    #Calculate the holding period return of each trade
    output_df = calc_position_return(output_df, slippage)

    #Calculate cumulative value of 1
    output_df = cum_value(output_df)

    #Calculate long_short_frequency
    output_df, n_long, true_long, n_short, true_short = long_short_freq(output_df)

    #Calculate Sharpe ratio
    annual_SR = 0
    annual_SR, annualized_mean_ex_ret = sharpe_ratio(cum_ret, annual_SR)

    #Calcuate max drawdown
    Max_Drawdown = 0
    Max_Drawdown = calc_max_drawdown(Max_Drawdown,cum_ret)

    #Calmar ratio - calculated by multiplying daily mean average return by number of trading days in the trading window divided by the Max Drawdown in the period
    Calmar = -annualized_mean_ex_ret/(Max_Drawdown/100)/(252*24)*df.shape[0] #Max drawdown is in percentage, annualised_mean_ex_ret is in percentage

    #Calculate period return
    n_rows = output_df.shape[0]
    period_return = ((output_df["Cumulative Value"].iloc[n_rows-1]/output_df["Cumulative Value"].iloc[0])-1)*100 #round period return to 4 dp

    #If our position is still open then adjust the position status
    if output_df["Action"].iloc[-1] == "Today":
        output_df["Position"].iloc[-1] = output_df["Position"].iloc[-2]

    #Convert Date to a better format to read
    action_table = output_df.copy()
    action_table["Action Date"]=pd.to_datetime(action_table["Action Date"])
    action_table["Action Date"] = action_table["Action Date"].dt.strftime("%Y-%m-%d %H:%M")

    cur_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-1])
    last_position = int(cum_ret["Position"].iloc[cum_ret.shape[0]-2]) #if the last position is the same of the current position, we must either be in a long position or in a short position
    print("last position:", last_position)
    print("current position:", cur_position)
    print("Last data point is", cum_ret["Date"].iloc[cum_ret.shape[0]-1]) #to check the time in BBG term

    if last_position == 0 and cur_position != 0: #if we opens a position now, send email
        print("we open a position now")
        open_position_signal(p, q, r, output_df, df, cum_ret, DD_limit) #provide a summary of the position we started
        #output_df

    elif last_position != 0 and cur_position == 0: #if we just closed our long or short position, send email
        print("we close a position now")
        close_position_signal(p, q, r, output_df, df, cum_ret, DD_limit) #provide a summary of the position we closed
        #output_df

    else: #if nothing, print current signal
        print("no action at", datetime.datetime.now())
        #print further actions
        no_signal(p, q, r, last_position, cur_position, output_df, df, cum_ret)

    #draw plot of price and action over the past 2 weeks for every scenario
    #plot graph
    plt.figure(figsize=(15,10))
    plt.plot(pd.Series(df["Date"]).to_numpy(), df['Close'].to_numpy(), label = "Close Price", linewidth = 3)
    plt.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Long'].to_numpy(), "y--", label = "Exit Price - Long")
    plt.plot(pd.Series(df["Date"]).to_numpy(), df['Exit Price - Short'].to_numpy(), "y--", label = "Exit Price - Short")
    plt.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Short'].to_numpy(), "r", label = "Entry Price - Short")
    plt.plot(pd.Series(df["Date"]).to_numpy(), df['Entry Price - Long'].to_numpy(), "g", label = "Entry Price - Long")

    for i in range(output_df.shape[0]):
        if i == 1:
            if output_df["Position"].iloc[i] == 1: #if long
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1: #if short
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

        elif i > 1: #if we do not have a position in day 1
            if output_df["Position"].iloc[i] == 1 and output_df["Position"].iloc[i-1] == 0: #we opened a new long position after day 1
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

            elif output_df["Position"].iloc[i] == -1 and output_df["Position"].iloc[i-1] == 0: #we opened a new short position after day 1
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == 1: #if we close a long position today
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "v", markerfacecolor = "magenta", markeredgecolor = "magenta", markersize = 8)

            elif output_df["Position"].iloc[i] == 0 and output_df["Position"].iloc[i-1] == -1: #if we close a short position today
                plt.plot(output_df["Action Date"].iloc[i], output_df["Action Price"].iloc[i], marker = "^", markerfacecolor = "lime", markeredgecolor = "lime", markersize = 8)

    plt.xlabel('datetime') # naming the x axis
    plt.ylabel('Spot price') # naming the y axis
    plt.title('USDCNH momentum strategy') # giving a title to my graph
    plt.legend() # show a legend on the plot
    plt.show() # function to show the plot
#%% No_signal, NOT IN USE, ONLY FOR REFERENCE
def no_signal_popup_window():
    root = tk.Tk()
    root.title("TRADE SIGNAL")
    root.geometry("900x700") #specify the size of the window

    #Create a text widget
    text = tk.Text(root, height = 8, width = 50)
    Font_tuple = ("Calibri", 12)
    text.configure(font = Font_tuple)
    text.pack()
    text.insert(tk.END, "No Action" + "\n")

    def close_window():
        root.destroy()

    #Close button
    close_button = tk.Button(root, text = "Close", command = close_window)
    close_button.pack()

    #Close after 10 seconds (10000 milliseconds)
    root.after(30000, close_window)

    # Start the Tkinter event loop
    tk.mainloop()
#%% #INPUT PARAMETERS THRESHOLD, RUN THE LOOP AND OBTAIN p, q, r
#Number of trading days to analyse
#window = 10 trading days, hourly data

#Assumption:
#1. We use Simple moving average over exponential moving average to limit the impact of price data of illiquid hours since we run the model at about 8am
#2. We apply a factor of 1.20 to the calculated standard deviation to account for the illiquid data points dragging down our standard deviation. The idea is that 7 hours out of 24 hours are less liquid so adj factor = sqrt((24-1)/(17-1))
#3. We try to "constrict" the parameter boundaries for inner and outer bands to try to make the distance between two bands more significant.
#4. In the BBG version we need to take away the last data point so that we have consistent data points
#5. We can try to increase loss tolerance to avoid our position from being "swing" away given volatile movements in USDCNH

#Standard deviation adj factor
SD_Adj = 1.20

#Bollinger Band Data to be taken in
data_pts_low = 20
data_pts_up = 60

#Outer Bollinger Bands
out_bb_sd_up = 200 #if you want to input 3, you write 300 'cap at 3.00 because anything beyond 3.00 looks too much to me
out_bb_sd_low = 150 #if you want to input 1.5, you write 150

#Inner Bollinger Bands
in_bb_sd_up = 80 #if you want to input 3, you write 300
in_bb_sd_low = 50 #if you want to input 1.5, you write 150

#Drawdown limit
DD_limit = 0.005 #0.005 = 0.5%

slippage = 0.0010 #set slippage to 10 pips per trade

#Currency pair and risk-free ticker from Bloomberg
fx_pair = 'CNH Curncy'
rf_rate = "USOSFRC Curncy"
period = 14 #window - the number of calendar days the start date is before the end date
#13 means 14 days, 14 means 15 days

p, q, r = run_loop(SD_Adj, data_pts_low,data_pts_up, out_bb_sd_up, out_bb_sd_low, in_bb_sd_up, in_bb_sd_low, DD_limit, slippage, fx_pair, rf_rate, period)

action_table, cum_ret = single_morning_run(p,q,r, SD_Adj, data_pts_up, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14)
# action_table

#schedule.every().hour.at(":02:30").do(lambda: single_recurring_run(p,q,r, SD_Adj, data_pts_up, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14))
#schedule.every().hours.at(":02").until("18:40").do(lambda: single_recurring_run(p,q,r, SD_Adj, data_pts_up, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14))
schedule.every().hours.at(":02").do(lambda: single_recurring_run(p,q,r, SD_Adj, data_pts_up, DD_limit, slippage, fx_pair = 'CNH Curncy', rf_rate = "USOSFRC Curncy", period = 14))
#wait for 10 sec before getting data because BBG may not update yet
#every 30th second

#time_now = datetime.datetime.now()
#while time_now.time() <= datetime.time(18, 00):
    #print("iterating at", time_now)
    #schedule.run_pending()
    #time.sleep(60)
    #time_now = datetime.datetime.now()

time_now = datetime.datetime.now()
while True: #time_now.time() <= datetime.time(18, 40):
    schedule.run_pending()
    time_now = datetime.datetime.now()
    #print("iterating at", time_now)
    time.sleep(1)
