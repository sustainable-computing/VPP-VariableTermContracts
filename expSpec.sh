python3 RunRLChargeWorld.py --agent PPO-sep --save-name month_sep_init_norm --save-agent --reward-coef 1 --proj-coef 0 --file-price df_price_2019_pad.csv --num-steps 24 --years 10 --anneal-lr True --update-epochs 10 --num-minibatches 1  && echo "Done month_agg!" &

