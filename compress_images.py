import os
from PIL import Image

def compress_webp(directory, quality=80):
    total_saved = 0
    
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.lower().endswith('.webp'):
                file_path = os.path.join(root, file)
                original_size = os.path.getsize(file_path)
                
                try:
                    with Image.open(file_path) as img:
                        # Save to a temporary file first
                        temp_path = file_path + '.temp'
                        # Save with optimization
                        # method=6 is the slowest but best compression
                        img.save(temp_path, 'WEBP', quality=quality, method=6)
                    
                    new_size = os.path.getsize(temp_path)
                    saved = original_size - new_size
                    
                    if saved > 0:
                        # If compressed file is smaller, replace original
                        os.replace(temp_path, file_path)
                        total_saved += saved
                        print(f"Compressed {file}: {original_size/1024:.2f}KB -> {new_size/1024:.2f}KB (Saved {saved/1024:.2f}KB)")
                    else:
                        # If not smaller, remove temp file and keep original
                        os.remove(temp_path)
                        print(f"Skipped {file}: Compressed size ({new_size/1024:.2f}KB) >= Original ({original_size/1024:.2f}KB)")
                        
                except Exception as e:
                    print(f"Error compressing {file}: {e}")
                    if os.path.exists(file_path + '.temp'):
                        os.remove(file_path + '.temp')

    print(f"\nTotal space saved: {total_saved/1024/1024:.2f}MB")

if __name__ == "__main__":
    assets_dir = os.path.join(os.getcwd(), 'static', 'viewer', 'assets')
    print(f"Compressing images in {assets_dir}...")
    compress_webp(assets_dir)
