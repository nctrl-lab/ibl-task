import math

# Monitor (IBL rig: 9.7" 4:3 LCD, LP097QX1 class)
RIG_WIDTH_CM = 19.7
RIG_DISTANCE_CM = 8.0
RIG_RESOLUTION = (2048, 1536)

# Gabor stimulus
SIGMA_DEG = 7.0
SIZE_DEG = 6 * SIGMA_DEG  # envelope ≈ 0 at edges
SF_CPD = 0.1
ORI_DEG = 0.0

# Trial timings — IBL trainingChoiceWorld spec
QUIESCENCE_MIN_S = 0.2  # offset; t = QUIESCENCE_MIN_S + x
QUIESCENCE_MAX_S = 0.5  # cap on t
QUIESCENCE_MEAN_S = 0.35  # mean of x ~ Exp; x truncated to [0, MAX-MIN]
QUIESCENCE_STILL_BAND_DEG = 2.0
RESPONSE_WINDOW_S = 60.0
OPEN_LOOP_HOLD_S = 1.0  # correct feedback (gabor at center)
ERROR_TIMEOUT_S = 2.0  # error feedback (gabor + white noise)
ITI_S = 0.5  # IBL: fixed 0.5s after stimulus offset

# Wheel / encoder
STIM_START_OFFSET_DEG = 35.0
ENCODER_PPR = 512
WHEEL_DIAMETER_MM = 62.0
WHEEL_GAIN_DEG_PER_MM = 4.0
COUNTS_PER_MM = ENCODER_PPR * 4 / (math.pi * WHEEL_DIAMETER_MM)
GAIN_DEG_PER_COUNT = WHEEL_GAIN_DEG_PER_MM / COUNTS_PER_MM

# Reward — valve-open time (ms) and dispensed water (µL).
REWARD_DEFAULT_MS = 50
REWARD_DEFAULT_UL = 3.0
MAX_VALVE_MS = 200

# Schedule
INITIAL_CONTRASTS = (1.0, 0.5)
EXPANSION_TIERS = (0.25, 0.125, 0.0625)
EXPANSION_ACCURACY = 0.80
EXPANSION_MIN_TRIALS = 400

# GUI contrast presets: the six training stages from the IBL protocol
# (Appendix 2, eLife 2021 Appendix 1—table 1b-c). Side is independently
# counter-balanced by TrainingSchedule; non-zero contrasts are sampled at
# twice the probability of 0% (paper: 2/11 vs 1/11) since the paper draws
# uniformly over signed trial types and ±c collapses to a single 0.
CONTRAST_PRESETS = (
    (1.0, 0.5),
    (1.0, 0.5, 0.25),
    (1.0, 0.5, 0.25, 0.125),
    (1.0, 0.5, 0.25, 0.125, 0.0625),
    (1.0, 0.5, 0.25, 0.125, 0.0625, 0.0),
    (1.0, 0.25, 0.125, 0.0625, 0.0),
)

# Countermeasure cue scheduler: window of recent choices to debias against,
# bucketed by the previous trial's (cue_side, outcome).
COUNTER_WINDOW_TRIALS = 8

# Mock hardware
MOCK_DEG_PER_KEY = 4.0  # arrow-key dev only

# Photodiode sync square
SYNC_PIX = 50
