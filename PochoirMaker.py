# ---------------------------------
# ----------- IMPORTANT -----------
#
# If you're not sure the required
# dependencies have been installed,
# execute this in a Python prompt:
#
# pip install opencv-python pillow numpy
#
# You only need to do this once.
# ---------------------------------

import cv2
import numpy as np
import sys, os
import tkinter as tk
from tkinter import filedialog

# =============================
# Pochoir editor — zones figées par changement de Threshold
# Version avec boîtes de dialogue Tkinter (Open… / Save As…)
# - Fenêtre viewport 1024x600 (crop strict, JAMAIS de resize/écrasement)
# - Clic gauche = peindre zone "en attente" (vert 50%)
#   → ces zones deviennent figées (bleu 50%) UNIQUEMENT quand la valeur
#     du curseur Threshold change. Elles ne bougent plus ensuite.
# - Clic droit + drag = panning du viewport
# - [E] = bascule pinceau/gomme (preview vert ⇄ rouge)
# - [S] = Save As… (sans overlay), [C] = clear (tout), [Esc]/[Q] = quitter
# - Curseurs : Threshold / Simplify / Median / Isolate(-1..1) / Brush (4–64 px)
# =============================

VIEW_W, VIEW_H = 1024, 600

# --- Lecture robuste (espaces, accents, etc.) ---
def safe_imread(filepath):
    try:
        with open(filepath, "rb") as f:
            data = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        print(f"Impossible d'ouvrir: {filepath}")
        print(e)
        return None

# --- Écriture robuste ---
def safe_imwrite(filepath, image):
    try:
        ext = os.path.splitext(filepath)[1]
        result, encoded = cv2.imencode(ext, image)
        if result:
            with open(filepath, "wb") as f:
                encoded.tofile(f)
            return True
        else:
            raise ValueError("cv2.imencode a échoué")
    except Exception as e:
        print(f"Impossible d'écrire: {filepath}")
        print(e)
        return False

# ---------- Choix du fichier via Tkinter ----------
root = tk.Tk()
root.withdraw()
input_path = filedialog.askopenfilename(
    title="Open image",
    filetypes=[("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff")]
)
if not input_path:
    print("Aucun fichier sélectionné. Fin.")
    sys.exit(0)

# ---------- Chargement image ----------
img = safe_imread(input_path)
if img is None:
    print("Impossible d'ouvrir:", input_path)
    sys.exit(1)

H, W = img.shape[:2]
base_name, _ = os.path.splitext(os.path.basename(input_path))
save_counter = 1

# ---------- États & buffers ----------
# Masques (0/255)
mask_pending = np.zeros((H, W), np.uint8)   # zones vertes (en attente de figer)
mask_frozen  = np.zeros((H, W), np.uint8)   # zones bleues (déjà figées)
# Valeurs finales binaires figées
frozen_result = np.zeros((H, W), np.uint8)

# Viewport (crop), panning
offset_x, offset_y = 0, 0
is_panning = False
last_mouse = None

# Dessin
is_drawing = False
last_draw_pt = None
mouse_pos = None   # pour preview
erase_mode = False # toggle avec touche 'E'

# --------- Fenêtre & trackbars ---------
# AUTOSIZE pour qu'OpenCV n'essaye JAMAIS de rescales l'image affichée
cv2.namedWindow("Pochoir", cv2.WINDOW_AUTOSIZE)
cv2.createTrackbar("Threshold", "Pochoir", 128, 255, lambda v: None)
cv2.createTrackbar("Simplify",  "Pochoir", 1,   20,  lambda v: None)
cv2.createTrackbar("Median",    "Pochoir", 0,   10,  lambda v: None)
cv2.createTrackbar("Isolate",   "Pochoir", 1,   2,   lambda v: None)  # 0..2 → -1..1
cv2.createTrackbar("Brush",     "Pochoir", 16,  64,  lambda v: None)  # clamp >=4

# Seuil précédent (pour déclencher le figé au changement)
last_threshold = cv2.getTrackbarPos("Threshold", "Pochoir")

# ---------- Fonctions ----------

def read_trackbars():
    if cv2.getWindowProperty("Pochoir", cv2.WND_PROP_VISIBLE) < 1:
        return None
    try:
        t  = cv2.getTrackbarPos("Threshold", "Pochoir")
        s  = cv2.getTrackbarPos("Simplify",  "Pochoir")
        m  = cv2.getTrackbarPos("Median",    "Pochoir")
        iso_raw = cv2.getTrackbarPos("Isolate",   "Pochoir")
        b  = max(4, cv2.getTrackbarPos("Brush",     "Pochoir"))
    except cv2.error:
        # Si on essaie de lire un trackbar alors que la fenêtre est fermée
        return None
    return t, s, m, (iso_raw - 1), b  # isolate ∈ {-1,0,1}


def apply_filters(gray_img, threshold, simplify, median, isolate):
    # Seuil binaire (sur niveaux de gris d'origine)
    _, bw = cv2.threshold(gray_img, threshold, 255, cv2.THRESH_BINARY)

    # Simplification (morpho ellipsoïde) après seuil
    if simplify > 0:
        k = 2 * simplify + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN,  kernel)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)

    # Médiane (arrondir contours)
    if median > 0:
        bw = cv2.medianBlur(bw, 2 * median + 1)

    # Isolement du plus grand contour (optionnel)
    if isolate != 0:
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            c = max(contours, key=cv2.contourArea)
            m = np.zeros_like(bw)
            cv2.drawContours(m, [c], -1, 255, -1)
            if isolate == 1:      # garder sujet (fond blanc)
                bw = cv2.bitwise_and(bw, m)
            elif isolate == -1:   # garder fond (sujet blanc)
                bw = cv2.bitwise_and(bw, cv2.bitwise_not(m))

    return bw

# Préparer niveaux de gris d'entrée une fois pour toutes
IMG_GRAY = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

# Pour geler au bon moment, on garde le rendu de la frame précédente
# (correspondant à last_threshold)
last_output = apply_filters(IMG_GRAY, last_threshold,
                            cv2.getTrackbarPos("Simplify", "Pochoir"),
                            cv2.getTrackbarPos("Median", "Pochoir"),
                            cv2.getTrackbarPos("Isolate", "Pochoir") - 1)


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def paint_circle(mask, center, brush):
    # disque plein avec rayon = brush
    cv2.circle(mask, center, brush, 255, -1)


def erase_circle(mask, center, brush):
    cv2.circle(mask, center, brush, 0, -1)


def mouse_cb(event, x, y, flags, param):
    global is_panning, last_mouse, offset_x, offset_y
    global is_drawing, last_draw_pt, mask_pending, mask_frozen, frozen_result, mouse_pos

    mouse_pos = (x, y)  # pour preview

    # Convertir coord. viewport -> coord. image
    ix = x + offset_x
    iy = y + offset_y

    # Si clic en dehors de l'image (zone noire du canvas), on ignore
    if ix < 0 or iy < 0 or ix >= W or iy >= H:
        if event in (cv2.EVENT_RBUTTONDOWN, cv2.EVENT_MOUSEMOVE) and is_panning and last_mouse is not None:
            # autoriser le pan même si la souris sort un peu
            dx = x - last_mouse[0]
            dy = y - last_mouse[1]
            offset_x = clamp(offset_x - dx, 0, max(0, W - VIEW_W))
            offset_y = clamp(offset_y - dy, 0, max(0, H - VIEW_H))
            last_mouse = (x, y)
        return

    if event == cv2.EVENT_LBUTTONDOWN:
        is_drawing = True
        last_draw_pt = (ix, iy)
        brush = max(4, cv2.getTrackbarPos("Brush", "Pochoir"))
        if erase_mode:
            # gomme: efface pending + frozen + frozen_result
            erase_circle(mask_pending, last_draw_pt, brush)
            erase_circle(mask_frozen,  last_draw_pt, brush)
            erase_circle(frozen_result, last_draw_pt, brush)
        else:
            paint_circle(mask_pending, last_draw_pt, brush)

    elif event == cv2.EVENT_MOUSEMOVE:
        if is_drawing:
            brush = max(4, cv2.getTrackbarPos("Brush", "Pochoir"))
            if erase_mode:
                cv2.line(mask_pending, last_draw_pt, (ix, iy), 0, 2*brush)
                cv2.line(mask_frozen,  last_draw_pt, (ix, iy), 0, 2*brush)
                cv2.line(frozen_result, last_draw_pt, (ix, iy), 0, 2*brush)
            else:
                cv2.line(mask_pending, last_draw_pt, (ix, iy), 255, 2*brush)
            last_draw_pt = (ix, iy)
        elif is_panning and last_mouse is not None:
            dx = x - last_mouse[0]
            dy = y - last_mouse[1]
            offset_x = clamp(offset_x - dx, 0, max(0, W - VIEW_W))
            offset_y = clamp(offset_y - dy, 0, max(0, H - VIEW_H))
            last_mouse = (x, y)

    elif event == cv2.EVENT_LBUTTONUP:
        is_drawing = False
        last_draw_pt = None

    elif event == cv2.EVENT_RBUTTONDOWN:
        is_panning = True
        last_mouse = (x, y)

    elif event == cv2.EVENT_RBUTTONUP:
        is_panning = False
        last_mouse = None

cv2.setMouseCallback("Pochoir", mouse_cb)

# ---------- Boucle principale ----------
while True:
    # Lire sliders
    vals = read_trackbars()
    if vals is None:
        break  # on quitte proprement si la fenêtre n'existe plus
    thresh, simpl, med, iso, brush = vals

    # Si le threshold change → figer ce qui est en pending avec le rendu précédent
    if thresh != last_threshold:
        # zones à figer = pending qui ne sont pas déjà figées
        new_freeze_zone = cv2.bitwise_and(mask_pending, cv2.bitwise_not(mask_frozen))
        if np.any(new_freeze_zone):
            idx = new_freeze_zone == 255
            frozen_result[idx] = last_output[idx]
            mask_frozen[idx] = 255
        # vider le pending (il devient visuellement bleu via mask_frozen)
        mask_pending[:] = 0
        last_threshold = thresh

    # Calculer le rendu courant avec le threshold actuel
    current_output = apply_filters(IMG_GRAY, thresh, simpl, med, iso)
    # Mettre à jour last_output pour le prochain cycle
    last_output = current_output.copy()

    # Combiner : zones figées + zones libres
    combined = current_output.copy()
    idx_frozen = mask_frozen == 255
    combined[idx_frozen] = frozen_result[idx_frozen]

    # Créer image couleur pour overlay
    display = cv2.cvtColor(combined, cv2.COLOR_GRAY2BGR)

    # Overlay bleu (zones figées)
    if np.any(idx_frozen):
        region = np.where(idx_frozen)
        disp_region = display[region]
        blue = np.full_like(disp_region, (255, 0, 0))
        disp_region = (disp_region * 0.5 + blue * 0.5).astype(np.uint8)
        display[region] = disp_region

    # Overlay vert (pending)
    idx_pending = mask_pending == 255
    if np.any(idx_pending):
        region = np.where(idx_pending)
        disp_region = display[region]
        green = np.full_like(disp_region, (0, 255, 0))
        disp_region = (disp_region * 0.5 + green * 0.5).astype(np.uint8)
        display[region] = disp_region

    # Viewport crop (aucun resize)
    x1, y1 = offset_x, offset_y
    x2, y2 = min(x1 + VIEW_W, W), min(y1 + VIEW_H, H)
    crop = display[y1:y2, x1:x2]

    # Canvas fixe 1024x600 (on colle le crop en haut-gauche)
    canvas = np.zeros((VIEW_H, VIEW_W, 3), np.uint8)
    canvas[:crop.shape[0], :crop.shape[1]] = crop

    # Preview de brosse (disque plein) vert (dessin) / rouge (gomme)
    if mouse_pos is not None and not is_drawing:
        px, py = mouse_pos
        if 0 <= px < VIEW_W and 0 <= py < VIEW_H:
            overlay = canvas.copy()
            color = (0, 0, 255) if erase_mode else (0, 255, 0)  # rouge si gomme, vert sinon (BGR)
            cv2.circle(overlay, (px, py), brush, color, -1)
            canvas = cv2.addWeighted(overlay, 0.3, canvas, 0.7, 0)

    cv2.imshow("Pochoir", canvas)

    # Si l'utilisateur ferme la fenêtre via la croix, on quitte proprement
    if cv2.getWindowProperty("Pochoir", cv2.WND_PROP_VISIBLE) < 1:
        break

    key = cv2.waitKey(20) & 0xFF
    if key in (27, ord('q')):
        break
    elif key == ord('s'):
        # Sauvegarde l'image pochoir sans overlay (zones figées incluses)
        out = current_output.copy()
        out[idx_frozen] = frozen_result[idx_frozen]
        # Boîte de dialogue Save As…
        save_path = filedialog.asksaveasfilename(
            title="Save As…",
            initialfile=f"{base_name}_pochoir_{save_counter:03d}.png",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg;*.jpeg"), ("All files", "*.*")]
        )
        if save_path:
            if safe_imwrite(save_path, combined):
                print(f"Image sauvegardée : {save_path}")
            save_counter += 1
    elif key == ord('c'):
        # Reset complet
        mask_pending[:] = 0
        mask_frozen[:]  = 0
        frozen_result[:] = 0
    elif key == ord('e'):
        erase_mode = not erase_mode
        print("Mode:", "GOMME (rouge)" if erase_mode else "PINCEAU (vert)")

cv2.destroyAllWindows()
