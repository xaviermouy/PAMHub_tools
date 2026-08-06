
from ecosound.soundscape.hmd import HMD
import cmocean.cm as cmo

import pandas as pd
import os
import matplotlib.pyplot as plt

if __name__ == '__main__':  # ← Add this line

    ## ####################################################################################################################
    deployment_dir =r'C:\Users\xavier.mouy\Documents\Projects\2024_NERACOOS_Soundscape_GOM\Analysis\data\HMD_pyPAM\NRS09\PMEL_SBNMS'
    outdir = r'C:\Users\xavier.mouy\Desktop'


    nc_filename_pattern = r'_\d{8}\.nc$'
    start_date_string = '2014-01-01'
    end_date_string = '2024-01-01'


    freq_bands = [[18, 22]]
    names = ['Fin_whale']
    spl_percentile = 50
    spl_time_interval = '1D'
    to_plot = 'L50'
    title = 'NRS09 - L50 (18-22 Hz)'
    outfilename = "NRS09_18-22Hz_L50_multiyear_overlayed_timeseries.png"


    ## Get SPL values #####################################################################################################
    with HMD(n_workers=8, memory_per_worker='8GB', use_processes=True) as hmd_cluster:

        # load nc files
        hmd_cluster.load_nc_files(deployment_dir,recursive=True,time_range=(start_date_string, end_date_string),filename_pattern=nc_filename_pattern,prefilter_by_date=True, chunks={'time': 1440, 'frequency': -1})
        hmd_cluster.summary()
        # Extract bands
        band_levels = hmd_cluster.extract_band_levels(freq_bands, band_names=names, persist=True)
        # # Calculate time series stats
        stats_timeseries = hmd_cluster.compute_timeseries_stats(band_levels, percentiles=[spl_percentile], resolution=spl_time_interval)

        # plot full time series
        #hmd_cluster.plot_timeseries(stats_timeseries[to_plot].compute(), title='Time series',save_path=f'{names[0]}_L{spl_percentile}_{spl_time_interval}.png',overlay=True)

        # Plot weekly data across years
        hmd_cluster.plot_multiyear_overlay(
            stats_timeseries[to_plot],
            time_axis='dayofyear',
            legend_loc='outside right top',
            linewidth=1,
            alpha=1,
            colors='Set2',
            ylabel=r'Sound Pressure Levels (dB re 1 $\mu$Pa)',
            xlabel='Time of year',
            show_median=True, median_color='black', median_linewidth=1.5,median_linestyle='--',
            #show_percentile_range=True, percentiles=[10, 90],range_color='lightgray', range_alpha=0.3,
            title=title,
            save_path=os.path.join(outdir, outfilename),
        )