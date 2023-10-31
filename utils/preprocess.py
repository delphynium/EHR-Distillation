from collections import Counter
import os
import pandas as pd
import numpy as np
from glob import glob
import re
from tqdm import tqdm

# mimic3 benchmark paper statistics
mimic3_benchmark_variable_list = [
    ("capillary_refill_rate", "0.0", "categorical"),
    ("diastolic_blood_pressure", 59.0, "continuous"),
    ("fraction_inspired_oxygen", 0.21, "continuous"),
    ("glascow_coma_scale_eye_opening", "4 spontaneously", "categorical"),
    ("glascow_coma_scale_motor_response", "6 obeys commands", "categorical"),
    ("glascow_coma_scale_total", "15", "categorical"),
    ("glascow_coma_scale_verbal_response", "5 oriented", "categorical"),
    ("glucose", 128.0, "continuous"),
    ("heart_rate", 86, "continuous"),
    ("height", 170.0, "continuous"),
    ("mean_blood_pressure", 77.0, "continuous"),
    ("oxygen_saturation", 98.0, "continuous"),
    ("respiratory_rate", 19, "continuous"),
    ("systolic_blood_pressure", 118.0, "continuous"),
    ("temperature", 36.6, "continuous"),
    ("weight", 81.0, "continuous"),
    ("ph", 7.4, "continuous"),
]

# form it into dictionary
mimic3_benchmark_variable_dict = {}
for name, avg, ftype in mimic3_benchmark_variable_list:
    mimic3_benchmark_variable_dict[name] = {"avg": avg, "type": ftype}

# for some columns that may contain abnormally big /small values, add boundaries
mimic3_benchmark_variable_dict["weight"]["bound"] = (0, 300)

def map_mimic3_benchmark_categorical_label(name, value):
    """
    Map categorical string labels that contain certain substring to integer labels. (starting from 1, 0 for unknown)
    """

    if pd.isna(value):
        return np.nan
    
    substring_mapping = {
        # "capillary_refill_rate": 0.0 and 1.0
        "glascow_coma_scale_eye_opening": {
            "respon": 1,
            "pain": 2,
            "speech": 3,
            "spont": 4,
        },
        "glascow_coma_scale_motor_response": {
            "respon": 1,
            "extens": 2,
            "flex": 3,
            "withd": 4,
            "pain": 5,
            "obey": 6,
        },
        # "glascow_coma_scale_total": 3.0 to 15.0
        "glascow_coma_scale_verbal_response": {
            "respon": 1,
            "trach": 1,
            "incomp": 2,
            "inap": 3,
            "conf": 4,
            "orient": 5,
        },
    }
    if name == "capillary_refill_rate":
        return int(value) + 1
    elif name == "glascow_coma_scale_total":
        return int(value) - 2
    else:
        for substring, label in substring_mapping[name].items():
            if substring in value.lower():
                return label
        return 0


def compute_feature_statistics(ts_dir, feature_dict):
    """
    Compute average values of each feature in all timeseries files under the given dir
    """
    
    # read all csvs and concat in one
    # ts_files = [f for f in os.listdir(ts_dir)]
    ts_files = glob(os.path.join(ts_dir, "*_episode*_timeseries.csv"))
    dfs = [pd.read_csv(file_path) for file_path in ts_files]
    df_in_one = pd.concat(dfs, ignore_index=True)

    # rename columns
    df_in_one.columns = df_in_one.columns.str.lower().str.replace(' ', '_')

    # get rid of abnormally big or small values
    for name in [name for name in feature_dict.keys() if "bound" in feature_dict[name].keys()]:
        lo, hi = feature_dict[name]["bound"]
        df_in_one.loc[(df_in_one[name] < lo) | (df_in_one[name] > hi), name] = np.nan
    
    continuous_col_names = [name for name in feature_dict.keys() if feature_dict[name]["type"] == "continuous"]
    categorical_col_names = [name for name in feature_dict.keys() if feature_dict[name]["type"] == "categorical"]
    
    # compute avg and std for continuous cols
    continuous_cols = df_in_one[continuous_col_names]
    avgs = continuous_cols.mean()
    stds = continuous_cols.std()

    # compute mode for categorical cols
    categorical_cols = df_in_one[categorical_col_names]
    # map strings to integer labels
    for name in categorical_col_names:
        categorical_cols.loc[:, name] = categorical_cols[name].map(lambda x: map_mimic3_benchmark_categorical_label(name, x))
    modes = categorical_cols.mode().iloc[0].astype(int)

    print(f"Continuous features' averages: \n{avgs}")
    print(f"Continuous features' standard deviations: \n{stds}")
    print(f"Categorical features' modes: \n{modes}")
    return avgs.to_dict(), stds.to_dict(), modes.to_dict()


def preprocess_ihm_timeseires(df: pd.DataFrame, feature_dict, normal_value_dict, resample_rate):
    """
    1. Get rid of anomalies;
    2. Resample;
    3. Impute;
    4. Normalize
    Save preprocessed data as new csv "<original_name>_clean.csv".

    Resample rate's unit is hour.
    Normal value dict is used to get the normal value for each column when imputing without a reference.
    """
    # rename columns
    df.columns = df.columns.str.lower().str.replace(' ', '_')

    # get rid of out-of-boundary values
    for name in [name for name in feature_dict.keys() if "bound" in feature_dict[name].keys()]:
        lo, hi = feature_dict[name]["bound"]
        df.loc[(df[name] < lo) | (df[name] > hi), name] = np.nan

    # resample 
    hour_bins = np.arange(0, 48+resample_rate, resample_rate)
    df["hour_bin"] = pd.cut(df["hours"], bins=hour_bins, labels=False, right=False) # return left value instead of interval labels; right is not closed
    df = df.groupby("hour_bin").last() # this will automatically set hour_bin as index, but some bins can be missing due to no data within that bin
    df = df.reindex(hour_bins[:-1]) # places NaN in locations having no value in the previous index
    df.drop(columns=["hours"], inplace=True)

    # modify categorical labels into integers
    for name in feature_dict.keys():
        if feature_dict[name]["type"] == "categorical":
            df.loc[:, name] = df[name].map(lambda x: map_mimic3_benchmark_categorical_label(name, x))

    # impute NaNs, add an extra birnary mask column for each feature column, 0 for imputed, 1 for real
    mask_df = df.notna().astype(int)
    mask_df.columns = [f"{name}_mask" for name in mask_df.columns]
    df.ffill(inplace=True) # foward fill
    # for columns still with NaNs, use prepared normal values
    for name in df.columns:
        if name in normal_value_dict:
            df[name].fillna(normal_value_dict[name], inplace=True)
    # add mask columns right after each featurn column
    for name in df.columns:
        mask_name = f"{name}_mask"
        mask_values = mask_df[mask_name].values
        df.insert(df.columns.get_loc(name)+1, mask_name, mask_values)
    
    return df


def preprocess_ihm_timeseries_files(ts_dir, output_dir, feature_dict, normal_value_dict, resample_rate=1, balance=False):
    """
    Proprosess all timeseries in folder ts_dir and output to output_dir, including labels
    If balance, output timeseries will be balanced in terms of classification, TODO
    """
    os.makedirs(output_dir, exist_ok=True)
    # ts_files = [f for f in os.listdir(ts_dir)]
    ts_files = glob(os.path.join(ts_dir, "*_episode*_timeseries.csv"))
    for file_path in tqdm(ts_files):
        re_match = re.match(r"(\d+)_episode(\d+)_timeseries.csv", os.path.basename(file_path))
        if not re_match:
            raise ValueError(f"Error parsing csv file: {file_path}")
        subject_id, episode_number = map(int, re_match.groups())
        df = pd.read_csv(file_path)
        df = preprocess_ihm_timeseires(df, feature_dict=feature_dict, normal_value_dict=normal_value_dict, resample_rate=resample_rate)
        df.to_csv(os.path.join(output_dir, f"{subject_id}_episode{episode_number}_clean.csv"))
    
    labels_path = os.path.join(ts_dir, "listfile.csv")
    labels_df = pd.read_csv(labels_path)
    labels_df['stay'] = labels_df['stay'].str.replace('_timeseries.csv', '')
    labels_df.to_csv(os.path.join(output_dir, "labels.csv"), index=False)