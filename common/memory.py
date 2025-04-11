from common.expired_dict import ExpiredDict

# 用户图片缓存，保存3分钟
USER_IMAGE_CACHE = ExpiredDict(60 * 3)

# 消息缓存，保存3分钟
MESSAGE_CACHE = ExpiredDict(60 * 3)