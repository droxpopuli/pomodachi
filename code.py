from os import mkdir, stat
import board
import time

import digitalio
import displayio
import neopixel
import terminalio
import adafruit_imageload
import math
import adafruit_pcf8523
from adafruit_debouncer import Debouncer
from adafruit_displayio_layout.layouts.grid_layout import GridLayout
from adafruit_display_text import label
from adafruit_bitmap_font import bitmap_font
from adafruit_display_shapes.circle import Circle

# Convenience shorthands
days = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
num_pixels = 2
num_back_pixels = 4
ORDER = neopixel.GRB
TICK_SIZE = (1.0/60.0)

# Runtime State Variables
delta_x = 1
delta_y = 2
both_down_time = -1
is_in_session = False
is_in_break = False
current_leg = -1
leg_break_start = -1
last_speech_clear = -1
pomo_gone = False

# Helper Functions
def color_wheel(pos):
    # Input a value 0 to 255 to get a color value.
    # The colours are a transition r - g - b - back to r.
    if pos < 0 or pos > 255:
        r = g = b = 0
    elif pos < 85:
        r = int(pos * 3)
        g = int(255 - pos * 3)
        b = 0
    elif pos < 170:
        pos -= 85
        r = int(255 - pos * 3)
        g = 0
        b = int(pos * 3)
    else:
        pos -= 170
        r = 0
        g = int(pos * 3)
        b = int(255 - pos * 3)
    return (r, g, b) if ORDER in (neopixel.RGB, neopixel.GRB) else (r, g, b, 0)

def rainbow_cycle(graphics, speed):
    graphics["neo_state"] += int(speed)
    for px_pos in range(num_pixels):
        pixel_index = (px_pos * 256 // num_pixels) + graphics["neo_state"]
        pixels[px_pos] = color_wheel(pixel_index & 255)
    pixels.show()

def setup_text(text, x_pos, y_pos, scale):
    fg = label.Label(terminalio.FONT, scale=scale, text=text, color=0xFFFFFF)
    fg.x = x_pos
    fg.y = y_pos
    return fg

def get_speech_bubble(state):
    return state["speech"]

def get_clock_time(state):
    t = state["time"]
    return "{} {:02d}:{:02d}".format(days[int(t.tm_wday)], t.tm_hour, t.tm_min)

def get_food_text(state):
    if state["hunger"] < 0:
        return "{} FUD (HRNGY!)".format(state["food"])
    else:
        return "{} FUD".format(state["food"])

def feed(state):
    if state["food"] > 0:
        state["food"] -= 1
        if state["hunger"] <= 0:
            state["hunger"] += state["hunger_multiplier"]
        else:
            state["hunger"] += 1
    state["speech"] = "*NOM*"

def hungry(state):
    state["hunger"] -= 1
    hunger_threshold = (state["food_for_leg"] * state["legs_in_session"] + state["food_for_session"]) * state["difficulty"]
    if state["hunger"] <= hunger_threshold:
        if state["food"] > 0:
            state["food"] -= 1
            state["hunger"] += 1
        else:
            state["run_away"] = True
    state["speech"] = "*GURGLE*"

def wander_face(graphics):
    state = graphics["state"]
    x = (2.0*math.sin(3 * state * 3.5) + 0.5*math.sin(4 * state * 1.2))/3.0
    y = (3.0*math.sin(3 * state * 1.3 + 0.4) + 1.0*math.sin(4 * state * 1.8))/3.0
    wandering_offset = (x, y)

    x_off = int(math.floor(wandering_offset[0]))
    y_off = int(math.floor(wandering_offset[1]))

    graphics["face"].x = x_off
    graphics["face"].y = y_off
    graphics["fg_text"].x = text_center_x + x_off
    graphics["fg_text"].y = text_center_y + y_off
    graphics["body"].x = int(math.floor(wandering_offset[0]) / 2.0)
    graphics["body"].y = BODY_OFFSET + int(math.floor(wandering_offset[0]) / 2.0)

def update_status_text(graphics, state):
    food_text = get_food_text(state)
    clock_text = get_clock_time(state)

    graphics["fg_food"].text = food_text
    graphics["fg_clock"].text = clock_text
    graphics["fg_text"].text = state["speech"]

    if state["hunger"] < 0:
        graphics["fg_food"].x = 45
        graphics["fg_food"].y= 15
    else:
        graphics["fg_food"].x = 75
        graphics["fg_food"].y = 15

    graphics["fg_clock"].x = 60
    graphics["fg_clock"].y = 185

def seconds_for_hunger(state):
    hunger_rate = state["eating_rate"][int(state["time"].tm_wday)]
    per_day = hunger_rate * state["session_rewards"]
    if per_day == 0:
        per_day = 1
    sfh = 24*60*60 // per_day
    return sfh

# Hardware Setup
i2c = board.I2C()
rtc = adafruit_pcf8523.PCF8523(i2c)
pixels = neopixel.NeoPixel(board.D9, num_pixels, brightness=0.8, auto_write=False, pixel_order=ORDER)
back_pixels = neopixel.NeoPixel(board.D8, num_back_pixels, brightness=0.1, auto_write=False, pixel_order=ORDER)
back_pixels[0] = (100, 100, 100)
back_pixels[1] = (50, 50, 50)
back_pixels[2] = (50, 50, 50)
back_pixels[3] = (100, 100, 100)
back_pixels.show()
display = board.DISPLAY
left_switch_pin = digitalio.DigitalInOut(board.D6)
left_switch_pin.direction = digitalio.Direction.INPUT
left_switch_pin.pull = digitalio.Pull.UP
switch_left = Debouncer(left_switch_pin, 0.01)
right_switch_pin = digitalio.DigitalInOut(board.D5)
right_switch_pin.direction = digitalio.Direction.INPUT
right_switch_pin.pull = digitalio.Pull.UP
switch_right = Debouncer(right_switch_pin, 0.01)

# Pomo State + Settings Dict and Operative Functions
pomo_state = {
    "hunger": 0,
    "food": 10,

    "length_of_leg": 1,
    "length_of_break": 1,
    "legs_in_session": 2,
    "food_for_leg": 1,
    "food_for_session": 4,
    # session_rewards
    "difficulty": 1,
    "hunger_multiplier": 3,
    "eating_rate": (0, 1, 2, 2, 2, 0, 0),

    "last_hunger": time.mktime(rtc.datetime),
    "time": rtc.datetime,
    "speech" : "Hi!",

    "run_away": False
}
pomo_state["session_rewards"] = (pomo_state["food_for_leg"] * pomo_state["legs_in_session"] + pomo_state["food_for_session"])


# Graphics Setup
bg = displayio.Bitmap(display.width, display.height, 1)
bg_palette = displayio.Palette(1)
bg_palette[0] = 0x000000
for x in range(display.width):
    for y in range(display.height):
        bg[x, y] = 0
bg_bitmap = displayio.TileGrid(bg, pixel_shader=bg_palette)
radius = 14
body = displayio.Bitmap(radius*2, radius*2, 2)
body_palette = displayio.Palette(2)
body_palette[0] = 0xe05347
body_palette[1] = 0xf0e310
body_palette.make_transparent(1)
for x in range(radius*2):
    for y in range(radius*2):
        if ((radius - y) ** 2) + ((radius - x) ** 2) < (radius ** 2):
            body[x, y] = 0
        else:
            body[x, y] = 1
body_bitmap = displayio.TileGrid(body, pixel_shader=body_palette)
BODY_OFFSET = 3
body_bitmap.y = BODY_OFFSET

face_sheet, sprite_palette = adafruit_imageload.load("img/faces.bmp",
                                                bitmap=displayio.Bitmap,
                                                palette=displayio.Palette)
sprite_palette.make_transparent(2)
face_bitmap = displayio.TileGrid(face_sheet, pixel_shader=sprite_palette,
                            width = 1, height = 1,
                            tile_width = 64, tile_height = 64,
                            default_tile = 0)
face_bitmap.x = 0
face_bitmap.y = 0
text_center_x = 70
text_center_y = 85
fg_speech = setup_text(get_speech_bubble(pomo_state), text_center_x, text_center_y, 2)
fg_clock = setup_text(get_clock_time(pomo_state), 0, 0, 2)
fg_food = setup_text(get_food_text(pomo_state), 0, 0, 2)
pomo_graphics = {
    "body": body_bitmap,
    "face": face_bitmap, 
    "fg_text": fg_speech, 
    "fg_food": fg_food,
    "fg_clock": fg_clock,
    "state": 0.0,
    "neo_state": 0
}

pomo_group = displayio.Group(scale=2, x=85, y=110)
pomo_group.append(body_bitmap)
pomo_group.append(face_bitmap)

advice_text = setup_text(" ", 0, 0, 2)
advice_text2 = setup_text(" ", 0, 0, 1)
advice_text3 = setup_text(" ", 0, 0, 1)

main_screen = displayio.Group()
main_screen.append(bg_bitmap)
main_screen.append(pomo_group)
main_screen.append(fg_clock)
main_screen.append(fg_food)
main_screen.append(fg_speech)

display.show(main_screen)
while True:
    # Update Internal and Hardware States
    pomo_state["time"] = rtc.datetime
    state = pomo_graphics["state"] + TICK_SIZE
    pomo_graphics["state"] = state
    switch_left.update()
    switch_right.update()
    if ((time.mktime(rtc.datetime) - pomo_state["last_hunger"]) >= seconds_for_hunger(pomo_state)):
        hungry(pomo_state)
        pomo_state["last_hunger"] = time.mktime(rtc.datetime)
        last_speech_clear = time.mktime(rtc.datetime) # HACKY

    # Update Generic Visuals
    cycle_speed = 10 if ((not switch_left.value) and (not switch_right.value)) else 2
    rainbow_cycle(pomo_graphics, cycle_speed)
    update_status_text(pomo_graphics, pomo_state)
    wander_face(pomo_graphics)

    # Speech Clearing
    if pomo_state["speech"] != "" and last_speech_clear == -1:
        last_speech_clear = time.mktime(rtc.datetime) 
    if last_speech_clear != -1:
        if ((time.mktime(rtc.datetime) - last_speech_clear) >= 3): 
            pomo_state["speech"] = ""
            last_speech_clear == -1

    if pomo_state["run_away"]:
        if not pomo_gone: 
            main_screen.remove(pomo_group)
            main_screen.remove(fg_food)
            main_screen.remove(fg_clock)
            main_screen.append(advice_text)
            main_screen.append(advice_text2)
            main_screen.append(advice_text3)
            advice_text.x = 35
            advice_text.y = 60
            advice_text2.x = 35
            advice_text2.y = 85
            advice_text3.x = 35
            advice_text3.y = 100
            pomo_gone = True
        advice_text.text = "Pomo is gone..."
        advice_text2.text = "There was not enough"
        advice_text3.text = "food for pomo."
        
    elif (not is_in_session): # Idle Time
        if (switch_left.fell and switch_right.fell):
            both_down_time = time.mktime(rtc.datetime)
        elif (switch_left.fell or switch_right.fell):
            feed(pomo_state)

        # Check Held Down Both Buttons for starting a pomo session
        if ((not switch_left.value) and (not switch_right.value) and (both_down_time != -1)):
            delta = time.mktime(rtc.datetime) - both_down_time
            pomo_state["speech"] = "Start in {:01d}".format(5 - delta)
            last_speech_clear = time.mktime(rtc.datetime) # HACKY
            if delta >= 6:
                # Start the Pomodoro Session
                pomo_state["speech"] = "POMO!"
                last_speech_clear = time.mktime(rtc.datetime) # HACKY
                both_down_time = -1
                pomo_group.scale = 1
                main_screen.remove(fg_food)
                main_screen.remove(fg_clock)
                # State Setup
                is_in_session = True
                leg_break_start = time.mktime(rtc.datetime)
                current_leg = 1
                is_in_break = False
        elif (both_down_time != -1.0):
            both_down_time = -1 # Button release reset
    else: # Pomodoro Time
        if not is_in_break: # Work Time Traveling
            if ((time.mktime(rtc.datetime) - leg_break_start) < (pomo_state["length_of_leg"] * 60)):
                pixels.brightness = 0.0
                if pomo_group.y + radius >= display.height - radius:
                    delta_y *= -1
                if pomo_group.x + radius >= display.width - radius:
                    delta_x *= -1
                if pomo_group.x - radius <= 0 - radius:
                    delta_x *= -1
                if pomo_group.y - radius <= 0 - radius:
                    delta_y *= -1
                pomo_group.x = pomo_group.x + delta_x
                pomo_group.y = pomo_group.y + delta_y
            else: # Signal That Work is Done
                pixels.brightness = 0.8
                pomo_state["speech"] = "Break Time!"
                if (not switch_left.value) and (not switch_right.value): # Press Button to progress....
                    leg_break_start = time.mktime(rtc.datetime)
                    pomo_state["food"] += pomo_state["food_for_leg"]
                    if current_leg < pomo_state["legs_in_session"]: # ...to a break.
                        is_in_break = True
                        main_screen.append(advice_text)
                        main_screen.append(advice_text2)
                        main_screen.append(advice_text3)
                    else:                                           # ... back home.
                        is_in_session = False
                        pomo_state["food"] += pomo_state["food_for_session"]
                        pomo_state["speech"] = "Good Job!"
                        last_speech_clear = time.mktime(rtc.datetime) # HACKY
                        pomo_group.x = 85
                        pomo_group.y = 110
                        pomo_group.scale = 2
                        main_screen.append(fg_food)
                        main_screen.append(fg_clock)
        else: # Break Time
            if ((time.mktime(rtc.datetime) - leg_break_start) < (pomo_state["length_of_break"] * 60)):
                pixels.brightness = 0.0
                pomo_group.y = 85
                pomo_group.x = 125
                pomo_group.scale = 2
                advice_text.text = "Pomo Says:"
                if (current_leg == 1):
                    advice_text2.text = "Maybe drink some"
                    advice_text3.text = "water, idk"
                elif  (current_leg == 2):
                    advice_text2.text = "Do a pushup or"
                    advice_text3.text = "two."
                elif  (current_leg == 3):
                    advice_text2.text = "Go for a small"
                    advice_text3.text = "walk!"
                else:
                    advice_text2.text = "You got this!"
                    advice_text3.text = " "
                advice_text.x = 35
                advice_text.y = 60
                advice_text2.x = 35
                advice_text2.y = 85
                advice_text3.x = 35
                advice_text3.y = 100
            else: # Signal That break Time Over
                advice_text.text = " "
                advice_text2.text = " "
                advice_text3.text = " "
                pomo_state["speech"] = "Round {}!".format(current_leg+1)
                pixels.brightness = 0.8
                if (not switch_left.value) and (not switch_right.value): # Press Button to End Break
                    main_screen.remove(advice_text)
                    main_screen.remove(advice_text2)
                    main_screen.remove(advice_text3)
                    leg_break_start = time.mktime(rtc.datetime)
                    is_in_break = False
                    current_leg += 1
                    pomo_group.scale = 1

    time.sleep(TICK_SIZE)





