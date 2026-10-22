
import cv2

for index in range(5):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)

    if cap.isOpened():
        ok, frame = cap.read()

        if ok and frame is not None:
            height, width = frame.shape[:2]
            print(f"Camera index {index}: available ({width}x{height})")
        else:
            print(f"Camera index {index}: opens, but no frame received")

    cap.release()
